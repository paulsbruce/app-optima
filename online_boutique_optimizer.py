#!/usr/bin/env python3
"""
Simplified optimization engine for the Online Boutique adservice.

Implements an automated Experiment -> Measure -> Optimize loop:
- Randomly samples Kubernetes resources and JVM parameters within valid ranges
- Applies the configuration to the adservice Deployment
- Waits for rollout readiness
- Runs a JMeter performance test
- Queries Prometheus for JMeter and container metrics
- Logs each iteration to CSV and JSON
- Selects the best acceptable configuration according to a configurable objective

This script is designed to satisfy the technical requirements described in the
Technical Challenge document.
"""

from __future__ import annotations

import argparse
import csv
import dataclasses
import json
import math
import os
import random
import shlex
import signal
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote_plus
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError


# -----------------------------
# Data model
# -----------------------------

MEM_FLOOR_MIB = 128
MEM_CELING_MIB = 512

@dataclass(frozen=True)
class ParameterRanges:
    cpu_request_m_min: int = 50
    cpu_request_m_max: int = 1000
    cpu_limit_m_min: int = 100
    cpu_limit_m_max: int = 2000
    memory_request_mib_min: int = MEM_FLOOR_MIB # 256
    memory_request_mib_max: int = MEM_CELING_MIB
    memory_limit_mib_min: int = MEM_FLOOR_MIB*2 # 512
    memory_limit_mib_max: int = MEM_CELING_MIB
    heap_mib_min: int = MEM_FLOOR_MIB # 256
    heap_mib_max: int = MEM_CELING_MIB
    gc_types: Tuple[str, ...] = (
        "UseSerialGC",
        "UseParallelGC",
        "UseG1GC",
    )


@dataclass(frozen=True)
class ExperimentConfig:
    cpu_request_m: int
    cpu_limit_m: int
    memory_request_mib: int
    memory_limit_mib: int
    heap_mib: int
    gc_type: str

    @property
    def java_opts(self) -> str:
        return f"-Xms{self.heap_mib}m -Xmx{self.heap_mib}m -XX:+{self.gc_type}" # -javaagent:/tmp/jmx_prometheus_javaagent-1.5.0.jar=9404:/tmp/jmx.config.yml"

    @property
    def cpu_request(self) -> str:
        return f"{self.cpu_request_m}m"

    @property
    def cpu_limit(self) -> str:
        return f"{self.cpu_limit_m}m"

    @property
    def memory_request(self) -> str:
        return f"{self.memory_request_mib}Mi"

    @property
    def memory_limit(self) -> str:
        return f"{self.memory_limit_mib}Mi"


@dataclass
class Metrics:
    throughput: Optional[float] = None
    response_time_ms: Optional[float] = None
    error_rate_pct: Optional[float] = None
    cpu_usage_cores: Optional[float] = None
    cpu_request_cores: Optional[float] = None
    cpu_limit_cores: Optional[float] = None
    memory_usage_mib: Optional[float] = None
    memory_request_mib: Optional[float] = None
    memory_limit_mib: Optional[float] = None


@dataclass
class IterationResult:
    iteration: int
    config: ExperimentConfig
    metrics: Metrics
    acceptable: bool
    objective_score: float
    rollout_status: str
    test_status: str
    started_at_epoch: float
    ended_at_epoch: float
    notes: str = ""

    def to_flat_dict(self) -> Dict[str, Any]:
        data = {
            "iteration": self.iteration,
            "started_at_epoch": self.started_at_epoch,
            "ended_at_epoch": self.ended_at_epoch,
            "duration_seconds": round(self.ended_at_epoch - self.started_at_epoch, 3),
            "cpu_request": self.config.cpu_request,
            "cpu_limit": self.config.cpu_limit,
            "memory_request": self.config.memory_request,
            "memory_limit": self.config.memory_limit,
            "heap_mib": self.config.heap_mib,
            "gc_type": self.config.gc_type,
            "java_opts": self.config.java_opts,
            "throughput": self.metrics.throughput,
            "response_time_ms": self.metrics.response_time_ms,
            "error_rate_pct": self.metrics.error_rate_pct,
            "cpu_usage_cores": self.metrics.cpu_usage_cores,
            "cpu_request_cores": self.metrics.cpu_request_cores,
            "cpu_limit_cores": self.metrics.cpu_limit_cores,
            "memory_usage_mib": self.metrics.memory_usage_mib,
            "memory_request_mib": self.metrics.memory_request_mib,
            "memory_limit_mib": self.metrics.memory_limit_mib,
            "acceptable": self.acceptable,
            "objective_score": self.objective_score,
            "rollout_status": self.rollout_status,
            "test_status": self.test_status,
            "notes": self.notes,
        }
        return data


# -----------------------------
# Exceptions / utilities
# -----------------------------


class CommandError(RuntimeError):
    pass


class PrometheusError(RuntimeError):
    pass


def log(message: str) -> None:
    print(time.strftime("[%Y-%m-%d %H:%M:%S]"), message, flush=True)


def run_cmd(
    cmd: List[str],
    timeout: int = 300,
    check: bool = True,
    capture_output: bool = True,
    env: Optional[Dict[str, str]] = None,
) -> subprocess.CompletedProcess:
    try:
        proc = subprocess.run(
            cmd,
            timeout=timeout,
            check=False,
            capture_output=capture_output,
            text=True,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise CommandError(f"Command timed out after {timeout}s: {' '.join(cmd)}") from exc

    if check and proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        stdout = (proc.stdout or "").strip()
        raise CommandError(
            f"Command failed ({proc.returncode}): {' '.join(cmd)}\nSTDOUT: {stdout}\nSTDERR: {stderr}"
        )
    return proc


def safe_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        val = float(value)
        if math.isnan(val) or math.isinf(val):
            return None
        return val
    except (TypeError, ValueError):
        return None


def median_or_none(values: List[Optional[float]]) -> Optional[float]:
    cleaned = [v for v in values if v is not None]
    if not cleaned:
        return None
    return statistics.median(cleaned)


# -----------------------------
# Sampler
# -----------------------------


class ConfigSampler:
    def __init__(self, ranges: ParameterRanges, rng: random.Random):
        self.ranges = ranges
        self.rng = rng

    def _step_choice(self, start: int, stop: int, step: int) -> int:
        values = list(range(start, stop + 1, step))
        return self.rng.choice(values)

    def sample(self) -> ExperimentConfig:
        # Generate limit first, then keep request <= limit.
        cpu_limit_m = self._step_choice(self.ranges.cpu_limit_m_min, self.ranges.cpu_limit_m_max, 100)
        cpu_request_m = self._step_choice(
            self.ranges.cpu_request_m_min,
            min(cpu_limit_m, self.ranges.cpu_request_m_max),
            100,
        )

        memory_limit_mib = self._step_choice(
            self.ranges.memory_limit_mib_min,
            self.ranges.memory_limit_mib_max,
            128,
        )
        memory_request_mib = self._step_choice(
            self.ranges.memory_request_mib_min,
            min(memory_limit_mib, self.ranges.memory_request_mib_max),
            128,
        )

        # Keep heap bounded by memory limit. Leave some headroom for non-heap.
        heap_ceiling = min(self.ranges.heap_mib_max, max(self.ranges.heap_mib_min, int(memory_limit_mib * 0.75)))
        heap_mib = self._step_choice(self.ranges.heap_mib_min, heap_ceiling, 128)
        gc_type = self.rng.choice(self.ranges.gc_types)

        return ExperimentConfig(
            cpu_request_m=cpu_request_m,
            cpu_limit_m=cpu_limit_m,
            memory_request_mib=memory_request_mib,
            memory_limit_mib=memory_limit_mib,
            heap_mib=heap_mib,
            gc_type=gc_type,
        )


# -----------------------------
# Kubernetes operator
# -----------------------------


class KubeOperator:
    def __init__(
        self,
        namespace: str,
        deployment: str,
        container_name: str,
        rollout_timeout_seconds: int,
        kubectl_bin: str = "kubectl",
    ):
        self.namespace = namespace
        self.deployment = deployment
        self.container_name = container_name
        self.rollout_timeout_seconds = rollout_timeout_seconds
        self.kubectl_bin = kubectl_bin

    def get_current_config(self) -> ExperimentConfig:
        jsonpath = (
            "{.spec.template.spec.containers[?(@.name=='%s')].resources.requests.cpu}|"
            "{.spec.template.spec.containers[?(@.name=='%s')].resources.limits.cpu}|"
            "{.spec.template.spec.containers[?(@.name=='%s')].resources.requests.memory}|"
            "{.spec.template.spec.containers[?(@.name=='%s')].resources.limits.memory}|"
            "{.spec.template.spec.containers[?(@.name=='%s')].env[?(@.name=='JAVA_OPTS')].value}"
        ) % ((self.container_name,) * 5)
        proc = run_cmd(
            [
                self.kubectl_bin,
                "-n",
                self.namespace,
                "get",
                "deployment",
                self.deployment,
                "-o",
                f"jsonpath={jsonpath}",
            ]
        )
        cpu_req, cpu_lim, mem_req, mem_lim, java_opts = (proc.stdout or "").strip().split("|", 4)
        mem_lim_mib = _parse_memory_mib(mem_lim) # int
        default_heap_size = int(mem_lim_mib * 0.5) if mem_lim_mib <= 256 else 127 if mem_lim_mib <= 512 else int(mem_lim_mib * 0.25)
        heap_mib = _parse_heap_mib(java_opts) or default_heap_size
        gc_type = _parse_gc_type(java_opts) or "UseG1GC"
        return ExperimentConfig(
            cpu_request_m=_parse_cpu_m(cpu_req),
            cpu_limit_m=_parse_cpu_m(cpu_lim),
            memory_request_mib=_parse_memory_mib(mem_req),
            memory_limit_mib=mem_lim_mib,
            heap_mib=heap_mib,
            gc_type=gc_type,
        )

    def apply_config(self, cfg: ExperimentConfig) -> None:
        patch = {
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": self.container_name,
                                "resources": {
                                    "requests": {
                                        "cpu": cfg.cpu_request,
                                        "memory": cfg.memory_request,
                                    },
                                    "limits": {
                                        "cpu": cfg.cpu_limit,
                                        "memory": cfg.memory_limit,
                                    },
                                },
                                "env": [
                                    {
                                        "name": "JAVA_OPTS",
                                        "value": cfg.java_opts,
                                    }
                                ],
                            }
                        ]
                    }
                }
            }
        }
        run_cmd(
            [
                self.kubectl_bin,
                "-n",
                self.namespace,
                "patch",
                "deployment",
                self.deployment,
                "--type=strategic",
                "-p",
                json.dumps(patch),
            ],
            timeout=120,
        )

    def wait_for_rollout(self) -> None:
        run_cmd(
            [
                self.kubectl_bin,
                "-n",
                self.namespace,
                "rollout",
                "status",
                f"deployment/{self.deployment}",
                f"--timeout={self.rollout_timeout_seconds}s",
            ],
            timeout=self.rollout_timeout_seconds + 30,
        )


def _parse_cpu_m(value: str) -> int:
    value = value.strip()
    if value.endswith("m"):
        return int(value[:-1])
    return int(float(value) * 1000)


def _parse_memory_mib(value: str) -> int:
    value = value.strip()
    if value.endswith("Gi"):
        return int(float(value[:-2]) * 1024)
    if value.endswith("Mi"):
        return int(float(value[:-2]))
    if value.endswith("M"):
        return int(float(value[:-1]))
    if value.endswith("G"):
        return int(float(value[:-1]) * 1024)
    return int(float(value) / (1024 * 1024))


def _parse_heap_mib(java_opts: str) -> Optional[int]:
    for token in shlex.split(java_opts or ""):
        if token.startswith("-Xmx") and token[-1].lower() == "m":
            return int(token[4:-1])
    return None


def _parse_gc_type(java_opts: str) -> Optional[str]:
    for token in shlex.split(java_opts or ""):
        if token.startswith("-XX:+Use"):
            return token.replace("-XX:+", "")
    return None


# -----------------------------
# Prometheus client
# -----------------------------


class PrometheusClient:
    def __init__(self, base_url: str, query_timeout_seconds: int = 20, retries: int = 3):
        self.base_url = base_url.rstrip("/")
        self.query_timeout_seconds = query_timeout_seconds
        self.retries = retries

    def instant_query(self, expr: str) -> Optional[float]:
        encoded = quote_plus(expr)
        url = f"{self.base_url}/api/v1/query?query={encoded}"
        last_err: Optional[Exception] = None
        for attempt in range(1, self.retries + 1):
            try:
                req = Request(url, headers={"Accept": "application/json"})
                with urlopen(req, timeout=self.query_timeout_seconds) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                if payload.get("status") != "success":
                    raise PrometheusError(f"Prometheus query failed: {payload}")
                result = payload.get("data", {}).get("result", [])
                if not result:
                    return None
                value = result[0].get("value", [None, None])[1]
                return safe_float(value)
            except (HTTPError, URLError, TimeoutError, PrometheusError, json.JSONDecodeError) as exc:
                last_err = exc
                if attempt < self.retries:
                    time.sleep(2 * attempt)
                else:
                    raise PrometheusError(f"Prometheus query failed after {self.retries} attempts: {expr}") from last_err
        return None


# -----------------------------
# Metrics collection
# -----------------------------


class MetricsCollector:
    def __init__(
        self,
        prometheus: PrometheusClient,
        namespace: str,
        deployment: str,
        container_name: str,
        jmeter_label_selector: str = 'job=~"jmeter"',
        metric_window: str = "2m",
    ):
        self.prometheus = prometheus
        self.namespace = namespace
        self.deployment = deployment
        self.container_name = container_name
        self.jmeter_label_selector = jmeter_label_selector
        self.metric_window = metric_window

    def collect(self) -> Metrics:
        metrics = Metrics()

        # JMeter metrics. Metric names vary between exporters; these are intentionally easy to override.
        metrics.throughput = self.prometheus.instant_query(
            f'sum(rate(Ratio_success{{{self.jmeter_label_selector}}}[{self.metric_window}]))'
        )
        metrics.response_time_ms = self.prometheus.instant_query(
            f'avg(rate(ResponseTime_sum{{code="200", {self.jmeter_label_selector}}}[{self.metric_window}])/rate(ResponseTime_count{{code="200", {self.jmeter_label_selector}}}[{self.metric_window}])>0)'
        )
        metrics.error_rate_pct = self.prometheus.instant_query(
            f'(avg(rate(Ratio_failure{{{self.jmeter_label_selector}}}[{self.metric_window}]))/avg(rate(Ratio_total{{{self.jmeter_label_selector}}}[{self.metric_window}])))*100'
        )

        pod_selector = (
            f'namespace="{self.namespace}",pod=~"{self.deployment}-.*"'#,container="{self.container_name}"'
        )

        metrics.cpu_usage_cores = self.prometheus.instant_query(
            f'sum(rate(container_cpu_usage_seconds_total{{{pod_selector}}}[{self.metric_window}]))'
        )
        metrics.memory_usage_mib = self._bytes_to_mib(
            self.prometheus.instant_query(
                f'sum(container_memory_working_set_bytes{{{pod_selector}}})'
            )
        )

        # Request/limit metrics from kube-state-metrics.
        req_selector = f'namespace="{self.namespace}",pod=~"{self.deployment}-.*",container="{self.container_name}"'
        metrics.cpu_request_cores = self.prometheus.instant_query(
            f'max(kube_pod_container_resource_requests{{{req_selector},resource="cpu",unit="core"}})'
        )
        metrics.cpu_limit_cores = self.prometheus.instant_query(
            f'max(kube_pod_container_resource_limits{{{req_selector},resource="cpu",unit="core"}})'
        )
        metrics.memory_request_mib = self._bytes_to_mib(
            self.prometheus.instant_query(
                f'max(kube_pod_container_resource_requests{{{req_selector},resource="memory",unit="byte"}})'
            )
        )
        metrics.memory_limit_mib = self._bytes_to_mib(
            self.prometheus.instant_query(
                f'max(kube_pod_container_resource_limits{{{req_selector},resource="memory",unit="byte"}})'
            )
        )
        return metrics

    @staticmethod
    def _bytes_to_mib(value: Optional[float]) -> Optional[float]:
        if value is None:
            return None
        return value / (1024 * 1024)


# -----------------------------
# Objective / acceptability
# -----------------------------


@dataclass(frozen=True)
class OptimizationPolicy:
    min_throughput: float
    max_error_rate_pct: float
    max_response_time_ms: Optional[float] = None

    def is_acceptable(self, metrics: Metrics) -> bool:
        if metrics.throughput is None or metrics.throughput < self.min_throughput:
            return False
        if metrics.error_rate_pct is None or metrics.error_rate_pct > self.max_error_rate_pct:
            return False
        if self.max_response_time_ms is not None:
            if metrics.response_time_ms is None or metrics.response_time_ms > self.max_response_time_ms:
                return False
        return True

    def score(self, config: ExperimentConfig, metrics: Metrics, baseline_throughput: Optional[float]) -> float:
        """
        Lower is better.

        Primary goal: minimize memory limit.
        Secondary tie-breakers:
          1. lower memory request
          2. higher throughput (negative because lower is better)
          3. lower response time
        """
        throughput_component = -(metrics.throughput or 0.0)

        if baseline_throughput is not None and metrics.throughput is not None and metrics.throughput < baseline_throughput: # if lower than baseline, penalize
            throughput_component = metrics.throughput * 100_000 # increase score (penalty) and scale to make it more significant than memory differences

        latency_component = metrics.response_time_ms or 1e9
        return (
            config.memory_limit_mib * 1_000_000
            + config.memory_request_mib * 1_000
            + latency_component
            + throughput_component
        )


# -----------------------------
# Persistence
# -----------------------------


class ResultStore:
    def __init__(self, csv_path: Path, json_path: Path):
        self.csv_path = csv_path
        self.json_path = json_path
        self.results: List[IterationResult] = []

    def append(self, result: IterationResult) -> None:
        self.results.append(result)
        self._write_csv()
        self._write_json()

    def _write_csv(self) -> None:
        self.csv_path.parent.mkdir(parents=True, exist_ok=True)
        rows = [r.to_flat_dict() for r in self.results]
        if not rows:
            return
        with self.csv_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)

    def _write_json(self) -> None:
        self.json_path.parent.mkdir(parents=True, exist_ok=True)
        payload = [r.to_flat_dict() for r in self.results]
        self.json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


# -----------------------------
# Optimization engine
# -----------------------------


class OptimizationEngine:
    def __init__(
        self,
        kube: KubeOperator,
        sampler: ConfigSampler,
        metrics_collector: MetricsCollector,
        policy: OptimizationPolicy,
        store: ResultStore,
        settle_seconds: int,
        iterations: int,
        restore_baseline: bool,
    ):
        self.kube = kube
        self.sampler = sampler
        self.metrics_collector = metrics_collector
        self.policy = policy
        self.store = store
        self.settle_seconds = settle_seconds
        self.iterations = iterations
        self.restore_baseline = restore_baseline
        self._stop_requested = False

    def request_stop(self, *_args: Any) -> None:
        log("Stop requested; finishing current iteration before exiting.")
        self._stop_requested = True

    def run(self) -> Dict[str, Any]:
        baseline = self.kube.get_current_config()
        log(f"Captured baseline config: {baseline}")
        baseline_metrics = self._collect_baseline_metrics()
        best: Optional[IterationResult] = None

        for i in range(0, self.iterations + 1):
            if self._stop_requested:
                break
            started_at = time.time()
            cfg = self.sampler.sample() if i > 0 else baseline
            annotation = "" if i>0 else "[baseline]"
            rollout_status = "success"
            test_status = "success"
            notes = ""
            metrics = Metrics()
            acceptable = False
            score = float("inf")

            log(f"Iteration {i}/{self.iterations}{annotation}: applying {cfg}")
            try:
                if i > 0:
                    self.kube.apply_config(cfg)
                    self.kube.wait_for_rollout()
                    if self.settle_seconds > 0:
                        log(f"Waiting {self.settle_seconds}s for metrics to stabilize")
                        time.sleep(self.settle_seconds)
                else:
                    log("Skipping rollout for baseline config already applied")
            except Exception as exc:
                rollout_status = "failed"
                notes = f"Rollout failure: {exc}"
                log(notes)
            else: # :DDD b/c AI said so (but also to avoid running JMeter if rollout failed :D, or do it in nested try)
                try:
                    log(f"Running jmeter for iteration {i}")
                    run_cmd(["./scripts/run_jmeter.sh"], timeout=(10 * 60))
                except Exception as exc:
                    test_status = "failed"
                    notes = f"JMeter failure: {exc}"
                    log(notes)
                finally:
                    try:
                        log(f"Collecting post-test metrics for iteration {i}")
                        metrics = self.metrics_collector.collect()
                    except Exception as exc:
                        notes = (notes + " | " if notes else "") + f"Prometheus collection failure: {exc}"
                        log(notes)

                acceptable = rollout_status == "success" and test_status == "success" and self.policy.is_acceptable(metrics)
                score = self.policy.score(cfg, metrics, baseline_metrics.throughput) if acceptable else float("inf")

            result = IterationResult(
                iteration=i,
                config=cfg,
                metrics=metrics,
                acceptable=acceptable,
                objective_score=score,
                rollout_status=rollout_status,
                test_status=test_status,
                started_at_epoch=started_at,
                ended_at_epoch=time.time(),
                notes=notes,
            )
            self.store.append(result)

            log(f"Iteration {i} results: acceptable={acceptable}, score={score}, metrics={metrics}")

            if acceptable and (best is None or result.objective_score < best.objective_score):
                best = result
                log(f"New best acceptable config found at iteration {i}")

        if self.restore_baseline:
            log("Restoring baseline configuration")
            try:
                self.kube.apply_config(baseline)
                self.kube.wait_for_rollout()
            except Exception as exc:
                log(f"WARNING: failed to restore baseline: {exc}")

        return self._build_summary(baseline, baseline_metrics, best)

    def _collect_baseline_metrics(self) -> Metrics:
        try:
            time.sleep(max(0, self.settle_seconds))
            return self.metrics_collector.collect()
        except Exception as exc:
            log(f"Baseline metrics collection failed: {exc}")
            return Metrics()

    def _build_summary(
        self,
        baseline_cfg: ExperimentConfig,
        baseline_metrics: Metrics,
        best: Optional[IterationResult],
    ) -> Dict[str, Any]:
        acceptable_results = [r for r in self.store.results if r.acceptable]
        summary: Dict[str, Any] = {
            "total_iterations": len(self.store.results),
            "acceptable_iterations": len(acceptable_results),
            "tests_began_at_epoch": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.store.results[0].started_at_epoch)) if self.store.results else None,
            "tests_ended_at_epoch": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.store.results[-1].ended_at_epoch)) if self.store.results else None,
            "baseline_config": asdict(baseline_cfg),
            "baseline_metrics": dataclasses.asdict(baseline_metrics),
            "best_configuration": None,
            "comparison_to_baseline": None,
        }
        if best is None:
            return summary

        comparison = {
            "memory_limit_reduction_pct": pct_change(
                baseline_cfg.memory_limit_mib,
                best.config.memory_limit_mib,
                invert=True,
            ),
            "memory_request_reduction_pct": pct_change(
                baseline_cfg.memory_request_mib,
                best.config.memory_request_mib,
                invert=True,
            ),
            "throughput_change_pct": pct_change(
                baseline_metrics.throughput,
                best.metrics.throughput,
                invert=False,
            ),
            "response_time_change_pct": pct_change(
                baseline_metrics.response_time_ms,
                best.metrics.response_time_ms,
                invert=True,
            ),
            "error_rate_change_pct": pct_change(
                baseline_metrics.error_rate_pct,
                best.metrics.error_rate_pct,
                invert=True,
            ),
        }
        summary["best_configuration"] = best.to_flat_dict()
        summary["comparison_to_baseline"] = comparison
        return summary


def pct_change(old: Optional[float], new: Optional[float], invert: bool) -> Optional[float]:
    if old in (None, 0) or new is None:
        return None
    change = ((new - old) / old) * 100.0
    return -change if invert else change


# -----------------------------
# CLI
# -----------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Optimize Online Boutique adservice by random search.")
    parser.add_argument("--namespace", default=os.getenv("NAMESPACE", "default"))
    parser.add_argument("--deployment", default=os.getenv("DEPLOYMENT", "adservice"))
    parser.add_argument("--container-name", default=os.getenv("CONTAINER_NAME", "server"))
    parser.add_argument("--iterations", type=int, default=int(os.getenv("ITERATIONS", "5")))
    parser.add_argument("--settle-seconds", type=int, default=int(os.getenv("SETTLE_SECONDS", "10")))
    parser.add_argument(
        "--rollout-timeout-seconds",
        type=int,
        default=int(os.getenv("ROLLOUT_TIMEOUT_SECONDS", "180")),
    )
    parser.add_argument(
        "--prometheus-url",
        default=os.getenv("PROMETHEUS_URL", "http://localhost:30900"),
        help="Prometheus base URL, e.g. http://<host>:30900",
    )
    parser.add_argument(
        "--result-dir",
        default=os.getenv("RESULT_DIR", "./results"),
    )
    parser.add_argument(
        "--csv-path",
        default=os.getenv("CSV_PATH", "./results/iterations.csv"),
    )
    parser.add_argument(
        "--json-path",
        default=os.getenv("JSON_PATH", "./results/iterations.json"),
    )
    parser.add_argument(
        "--summary-path",
        default=os.getenv("SUMMARY_PATH", "./results/summary.json"),
    )
    parser.add_argument(
        "--random-seed",
        type=int,
        default=int(os.getenv("RANDOM_SEED", "42")),
    )
    parser.add_argument(
        "--min-throughput",
        type=float,
        default=float(os.getenv("MIN_THROUGHPUT", "1.0")),
    )
    parser.add_argument(
        "--max-error-rate-pct",
        type=float,
        default=float(os.getenv("MAX_ERROR_RATE_PCT", "1.0")),
    )
    parser.add_argument(
        "--max-response-time-ms",
        type=float,
        default=float(os.getenv("MAX_RESPONSE_TIME_MS", "0")),
        help="0 disables latency constraint",
    )
    parser.add_argument(
        "--jmeter-label-selector",
        default=os.getenv("JMETER_LABEL_SELECTOR", 'job="jmeter"'),
        help='Raw label selector body used in PromQL, e.g. job="jmeter",test="boutique"',
    )
    parser.add_argument(
        "--metric-window",
        default=os.getenv("METRIC_WINDOW", "2m"),
    )
    parser.add_argument(
        "--restore-baseline",
        action="store_true",
        default=os.getenv("RESTORE_BASELINE", "true").lower() in {"1", "true", "yes"},
    )
    parser.add_argument(
        "--no-restore-baseline",
        action="store_false",
        dest="restore_baseline",
    )
    return parser


def validate_args(args: argparse.Namespace) -> None:
    if args.iterations < 1:
        raise SystemExit("--iterations must be at least 1 to satisfy the challenge requirement.")


def main() -> int:
    args = build_parser().parse_args()
    validate_args(args)

    rng = random.Random(args.random_seed)
    ranges = ParameterRanges()
    sampler = ConfigSampler(ranges, rng)

    kube = KubeOperator(
        namespace=args.namespace,
        deployment=args.deployment,
        container_name=args.container_name,
        rollout_timeout_seconds=args.rollout_timeout_seconds,
    )
    prometheus = PrometheusClient(args.prometheus_url)
    metrics_collector = MetricsCollector(
        prometheus=prometheus,
        namespace=args.namespace,
        deployment=args.deployment,
        container_name=args.container_name,
        jmeter_label_selector=args.jmeter_label_selector,
        metric_window=args.metric_window,
    )
    max_rt = None if args.max_response_time_ms <= 0 else args.max_response_time_ms
    policy = OptimizationPolicy(
        min_throughput=args.min_throughput,
        max_error_rate_pct=args.max_error_rate_pct,
        max_response_time_ms=max_rt,
    )
    store = ResultStore(Path(args.csv_path), Path(args.json_path))

    engine = OptimizationEngine(
        kube=kube,
        sampler=sampler,
        metrics_collector=metrics_collector,
        policy=policy,
        store=store,
        settle_seconds=args.settle_seconds,
        iterations=args.iterations,
        restore_baseline=args.restore_baseline,
    )

    signal.signal(signal.SIGINT, engine.request_stop)
    signal.signal(signal.SIGTERM, engine.request_stop)

    summary = engine.run()
    summary_path = Path(args.summary_path)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    log("Optimization complete.")
    if summary.get("best_configuration"):
        best = summary["best_configuration"]
        log(
            "Best configuration: "
            f"memory_limit={best['memory_limit']}, "
            f"memory_request={best['memory_request']}, "
            f"cpu_request={best['cpu_request']}, "
            f"cpu_limit={best['cpu_limit']}, "
            f"heap_mib={best['heap_mib']}, gc_type={best['gc_type']}, "
            f"throughput={best['throughput']}, response_time_ms={best['response_time_ms']}, "
            f"error_rate_pct={best['error_rate_pct']}"
        )
    else:
        log("No acceptable configuration found.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
