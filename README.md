# AppOptima

This repository is an example of automated performance tuning by adjustment of kubernetes pod resources and load test results.

Jump to:

- [Key Findings](#key-findings)
- [Recommendations](#recommendations)
- [Summary Report](results-20260313_50i_ramp/report.html)
- [What's Missing from this Research Spike](#whats-missing-from-this-research-spike)

# Business Objective

The main goal of this example is to ***demonstrate a homegrown approach*** *to identifying the least resources to maintain the current throughput* of a given component ('[adservice](https://github.com/GoogleCloudPlatform/microservices-demo/tree/main/src/adservice)'). It is important to identify infrastructure that is over-provisioned and apply right sizing so that costs are kept to a necessary minimum.

# Fully Automated

After completion, it should provide a recommended configuration such that this could later be applied after some approval process involving humans or a predefined auto-approval process.

Other than setting the baseline environment (usually via an initial setup deployment script), this experimentation process (as well as report generation) should be easy to incorporate into a fully automated build/test/deploy process such as CI/CD or scheduled maintenance windows.

# High-level Process Overview

At a high level, an experimentation pass requires multiple rounds (a.k.a. 'iterations') of configuration, testing, and metrics collection using the same workload on difference resource tunings. These results are compared to a baseline run's resource usage and scored against a primary goal (e.g. maintaining throughput).

1. Runs a pre-specified load test against the configuration in place to generate a baseline
2. Captures all results into a JSON (processing-friendly) 'iterations' file
3. Generates configurations that use appropriately bounded random values for CPU, memory, and JVM heap size values
4. Runs a statistically significant number of iterations over applying a configuration, running tests, and capturing important metrics
5. Uses captured metrics to 'score' each test/configuration result, then recommends the one with the best score

# Environment Caveats

- There could be other goals than throughput such as Response Time, but throughput of user transactions is imperative to the business
- The target service/component is only one of many components involved in the web application being tested
- The target application is containerized and running in kubernetes, so host resources are shared between components
- The infrastructure host node is running a non-production version of kubernetes for testing purposes
- Performance spike, soak, endurance, and threshold testing is out of scope, since the goal is to maintain current performance, not resiliency testing
- The above tests should also be considered periodically to better inform results under long-running and production-like conditions (e.g. saturation)
- Prometheus instance retention is only 6 hours, so no historicals beyond what was captured at point in time of testing
- Component under test (adservice) is Java (openjdk 19) based with adequate containerized defaults, so legacy configuration such as 'UseContainerSupport', 'ActiveProcessorCount', etc. do not apply (only for legacy versions of Java)

# Acceptability of Configurations for Recommended New 'Best'

Given the importance of user business transactions such as 'AddToCart' and 'Checkout', throughput is the primary constraint to determining 'better' configurations, but 'best' depends on how reduced the memory and compute requirements are of a given configuration.

In all cases, recommendations are only provided from the pool of 'acceptable' candidate configurations, defined by the following criteria:

- Iterations resulting in lower throughput than the original baseline (as-is system expectations) are considered 'not acceptable'
- Tests that result in an Error Rate greater than 1% (industry common) are listed, but not used; scripts needed to be fixed
- If analysis-critical performance metrics cannot be captured, this precludes the iteration from acceptability

# Scoring Algorithm

Given the constraint to maintain existing throughput, scoring iterations and acceptable configurations which do so involves both resource requirements and even the resulting effects on response time (from test client's perspective; network latency + server processing).

    Primary goal: minimize memory limit.
    Secondary tie-breakers:
        1. lower memory request
        2. higher throughput (negative because lower is better)
        3. lower response time

# Improving Workload 'Unit Cost' to Refine Cloud Spend

While maintaining the baseline throughput is a business constraint, within those operating guidelines minimizing the required configuration of memory and compute limits for individual workload pods helps to more tightly pack pods into host resources. This provides a more accurate understanding of the 'unit cost' associated with particular service components comprising the application...which in turn allows better selection of cloud-specific instance types for specific components and their auto-scaling expectations.

Additionally, instances with smaller provisioning requirements on CPU and memory often scale faster and cost less than large, bloated instances. Cloud-specific container providers such as AWS Fargate also impose lifecycle limits on their inexpensive on-demand resources, so dynamic re-scaling and fast spin-up time on service components often play a critical role in the availability and resilience of applications. Driving workload 'unit cost' down to the most reasonable levels, given business constraints, is a cost-efficiency step that should not be overlooked.

# Key Findings

- 50-iteration overnight 6+ hour test (for statistical significance)
    - [Summary Report](results-20260313_50i_ramp/report.html)
- 15-iteration 1 hour test (for minimum proof and validation of 50-iteration test)
    - [Summary Report](results-20260315_15i_ramp/report.html)
- Baseline (as-is) configuration seem to be arbitrarily set to non-uniform values
    - Current: 200-300m CPU request-limit and 180-300Mi Memory request-limit
- Test Findings
    - G1GC (Garbage-first) seems to win slightly over Serial (low-resource environments); Parallel (good for batch processing) should not be used
    - Constrained CPU results in linear latency (throughput) 90th percentile (user experience)
    - Matching heap configuration to pod resources improves immediate and longer responsiveness
    - Smaller resource requests (smaller pods) better for node-to-pod packing if autoscaling is used

# Recommendations

If the goal is simply to maintain current throughput (avg. 88TPS), then:

- Use G1GC (all around better for generic performance situations)
- Set initial and total heap size values to at least 50% (or 80%) minus system overhead
- Adjust memory request and limit to lower, more uniform values (128Mi, 256Mi)
- Widen CPU request and limit slightly; though compute more expensive in cloud, this will thrash less and average cycles is same/less than baseline

However, if we also need to accommodate higher number of users, linearly increasing response times will be a problem. To address this,

- Implement Horizontal Pod Autoscaling for pod provisioning and scaling flexibility; smaller resources are better for this purpose too
- Increasing the CPU request slightly and limit to higher values (250m, 700m) with negligible results on average use; reduces spikes

In all cases, we should also ensure that we:

- Perform these experiments continuously (semi-frequently) in 'lower' environment to catch obvious misconfigurations
- Pair with progressive rollout techniques; apply to some resources in production and compare resulting costs before rolling out as new default
- Consider breaking out services with unique performance characteristics to specialized container hosting setups optimized for each component
- Look for external solutions that allow us to focus on app and infra optimization rather than homegrown tooling maintenance

**Anticipated outcomes**:

- Better pod-packing characteristics with predictable unit cost for auto-scale and provisioning pricing models
- Cost savings: anecdotal until production proof, however ~10-15% predicted (despite limits increase) given G1GC adjustments and lower avg use
- If new compute costs outweigh savings on memory, pod packing consolidations, or autoscaling variability savings, then we know we're at lowest cost to maintain current throughput (a.k.a. 'at capacity'...which itself is not a safe place to be)

> "CPU is generally more expensive than RAM in cloud provisioning, often making up about 88% of the total instance cost compared to 12% for memory. While memory is necessary, compute-optimized instances (high CPU) usually command higher prices per hour than memory-optimized ones, as CPUs are more complex and costly to manufacture." - [Cast.ai](https://cast.ai/blog/how-to-calculate-cpu-vs-memory-costs-for-more-accurate-k8s-cost-monitoring/)

# Additional Notes

## What's Missing from this Research Spike

- CI/CD pipeline script examples, though .py scripts aren't hard to run as shell commands
- Support for other commonly used runtimes (e.g. Node.js)
- Consolidated...
    - Reporting: Where are we going to store these results for historical comparisons?
    - Comparisons: How do we compare from last known good configuration results?
    - Repeatability: Specialized values, configuration, dashboards, and recommendation analysis for each new service (just Java, not even Node)
    - Maintainability: Many threshold and 'special values' buried in code and arguments at runtime
    - Accuracy: No standard for experiment configuration values (harder to maintain over time)
    - Production-likeness: No parallel view of production configuration and values (no source of truth)
    - Scalability: No common interface for config, dashboards, and permissions (hard to scale in the org, training etc.)

## Added Adservice JMX monitoring for Testing

For additional tuning and diagnostic purposes during testing, the JMX exporter for Prometheus (client) was added to the 'adservice' deployment. This enables tracking of Java garbage collection health as well as many other service-related metrics during testing.

- jvm_gc_collection_seconds_count
- JVM-specific memory an thread use; [good reference on Akamas site](https://docs.akamas.io/akamas-docs/reference/telemetry-metric-mapping/prometheus-metrics-mapping#jvm)

## Interesting Resources

- [Optimizing Resource Usage in Kubernetes by Carlos Sanchez - Devoxx 2025](https://www.youtube.com/watch?v=MIk6kkBGk8E)
- [What Happens When You Run Java at Scale on Kubernetes](https://medium.com/javarevisited/what-happens-when-you-run-java-at-scale-on-kubernetes-2ac6db3b140f)