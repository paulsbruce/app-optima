
import asyncio
import json
import sys
import logging
from prometheus_api_client import PrometheusConnect

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

namespace = "akamas-online-boutique"
deployment = "adservice"
container = "server"

async def collect_prom_stats():
    stats_fmt = {
        "jmeter_response_time": 'avg(last_over_time(ResponseTime{label=~".*",quantile="0.9"}[10m]))',
        "jmeter_transaction_success": 'sum(rate(Ratio_success{job=~"jmeter"}[10m]))',
        "jmeter_transaction_failure": 'sum(rate(Ratio_failure{job=~"jmeter"}[10m]))',
        "cpu_usage": '1e3 * avg(rate(container_cpu_usage_seconds_total{container="", namespace=~"___namespace___", pod=~"___deployment___.+" }[10m]))',
        "cpu_requests": '1e3 * avg(sum by (pod) (kube_pod_container_resource_requests{resource="cpu", namespace=~"___namespace___", pod=~"___deployment___.+" }))',
        "cpu_limits": '1e3 * avg(sum by (pod) (kube_pod_container_resource_limits{resource="cpu", namespace=~"___namespace___", pod=~"___deployment___.+" }))',
        "memory_usage": 'avg(last_over_time(container_memory_usage_bytes{container="", namespace=~"___namespace___", pod=~"___deployment___.+" }[10m]))',
        "memory_requests": '1e3 * avg(sum by (pod) (kube_pod_container_resource_requests{resource="memory", namespace=~"___namespace___", pod=~"___deployment___.+" }))',
        "memory_limits": '1e3 * avg(sum by (pod) (kube_pod_container_resource_limits{resource="memory", namespace=~"___namespace___", pod=~"___deployment___.+" }))',
    }
    stats_queries = {key: replace_all_tokens(expr) for key, expr in stats_fmt.items()}
    #logger.debug(stats_queries)
    logger.info("\nCollecting stats:")

    for key, query in stats_queries.items():
        stats = await collect_stats(query)
        logger.info(f"{len(stats)} Stats for {key}: {stats}")    

def replace_all_tokens(expr,replaces={}):
    default_replaces = {
        "___namespace___": namespace,
        "___deployment___": deployment,
        "___container___": container,
    }
    replaces = {**default_replaces, **replaces}
    ret = f'{expr}'
    for key, value in replaces.items():
        ret = ret.replace(key, value)
    return ret

async def collect_stats(query):
    # Here you would implement the logic to query Prometheus using the stats_queries defined in collect_prom_stats
    # For example, you could use the prometheus_api_client library to query Prometheus and collect the metrics
    PROM_URL = 'http://paul.lab.akamas.io:30900'
    prom = PrometheusConnect(url=PROM_URL, disable_ssl=True)

    # Fetch data using a custom PromQL query
    custom_data = prom.custom_query(query=query)

    return custom_data

async def main():

    # generate configs
    configs = generate_configs()
    logger.info(f"Generated {len(configs)} configurations for testing.")
    logger.info(f"{configs.keys().__iter__().__next__()} is an example configuration key.")
    for key, cmd in configs.items():
        logger.info(f"\n# --- {key} ---:\n{cmd}\n")

    # run a first test to establish baseline metrics
        # collect prometheus stats

    #await run_experiment()
    # run experiments (long-running)
        #await collect_prom_stats()
        # collect prometheus stats after each
    
    # consolidate results and generate report

# def get_patch_cmd(section, key):
#     patch = '{"spec": {"template": {"spec": {"containers": [{"name": "___container___", "resources": {___resources_content___}}}]}}}}'
#     replaces = {
#         "___section___": section,
#         "___key___": key,
#     }
#     patch = replace_all_tokens(patch, replaces)

#     cmd = f"kubectl patch deployment '{deployment} -p '{patch}'"
#     return cmd

def generate_configs():
    # mods_templates = {
    #     'cpu_requests': get_patch_cmd('requests','cpu'),
    #     'limits_requests': get_patch_cmd('limits','cpu'),
    # }
    dims = {
        'resource': ['cpu','memory'],
        'section': ['requests','limits'],
    }
    cpu_domain = [50, 100] # in millicores
    cpu_req_lim_multiplier = [2,3]
    mem_domain = [128, 256] # in Mi
    mem_req_lim_multiplier = [1.75, 2.5]
    
    jvm_heap_percent_domain = [0.25, 0.7, 1] # in percentage of memory limit
    jvm_gc_type_domain = ['-XX:+UseG1GC', '-XX:+UseZGC'] # -XX:+UseParallelGC is long pause, good for batch, not for web

    # throw out any out-of-range values given the cluster resources

    cpu_only_patches = {}
    for cpu in cpu_domain:
        for multiplier in cpu_req_lim_multiplier:
            vals = {
                "___req___": f"{cpu}m",
                "___lim___": f"{int(cpu*multiplier)}m",
            }
            cpu_only_patches[f"cpu_{cpu}_{multiplier}"] = replace_all_tokens(
                '{"requests": {"cpu": "___req___"}, "limits": {"cpu": "___lim___"}}',vals
            )
    
    mem_only_patches = {}
    for mem in mem_domain:
        for multiplier in mem_req_lim_multiplier:
            vals = {
                "___req___": f"{mem}Mi",
                "___lim___": f"{int(mem*multiplier)}Mi",
            }
            mem_only_patches[f"memory_{mem}_{multiplier}"] = replace_all_tokens(
                '{"requests": {"memory": "___req___"}, "limits": {"memory": "___lim___"}}',vals
            )

    all_patches = cpu_only_patches#in favor of pairing with heap# | mem_only_patches

    heap_only_patches = {}
    for percent in jvm_heap_percent_domain:


    # logging.info(f"Generated {len(cpu_only_patches)} CPU-only patches.")
    # logging.info(f"{cpu_only_patches}")

    mods = {key: replace_all_tokens("""
            kubectl patch deployment ___deployment___ -n ___namespace___ --type='strategic' -p '{"spec": {"template": {"spec": {"containers": [{"name": "___container___", "resources": ___resources_content___}]}}}}'
            """,{"___resources_content___": val}).strip() for key,val in cpu_only_patches.items()}
    
    
    return mods

async def run_experiment():


    logger.info("JMeter test starting...")
    statsDoc, success, logs = await runJMeterTest()

    if success:
        logger.info("JMeter test completed successfully. Statistics:")
        logger.info(json.dumps(statsDoc, indent=2))
    else:
        logger.error("JMeter test failed. Logs:")
        logger.error(logs)

async def runJMeterTest():
    # This function would contain the logic to run the JMeter test, similar to the runLocalJMeterTest function that was commented out in the original code.
    # You would need to implement the logic to execute the JMeter test, collect the output, and parse the statistics from the output.
    pass

if __name__ == "__main__":
    asyncio.run(main())


#async def runLocalJMeterTest():

#     retvar = (None, False, None)

#     testDuration = 5
#     host = "10.66.66.2"
#     port = "5002"
#     path = "/"

#     shcmd = f"""
# kubectl delete pod jmeter-test --ignore-not-found; 
# JMX_CONTENT=$(cat ./uritest.jmx); kubectl run jmeter-test --image=alpine/jmeter --env JMX_CONTENT="$JMX_CONTENT" --command -- /bin/sh -c 'echo "Testing..." && mkdir -p /j && echo $JMX_CONTENT > /j/uritest.jmx && /entrypoint.sh -n -t /j/uritest.jmx -l /results.json -e -o /agg -f -JtestDuration={testDuration} -Jhost={host} -Jport={port} -Jpath={path}  && echo "<statistics.json>" && cat /agg/statistics.json && echo -e "</statistics.json>" '; 
# kubectl wait --for=condition=Ready pod/jmeter-test --timeout 60s; 
# kubectl logs -f jmeter-test; 
# kubectl delete pod jmeter-test --ignore-not-found 2>/dev/null;
# """.strip()

#     commands = shcmd.split("\n")
#     outs = []
#     for cmd in commands:
#         logger.debug(f"Running command: {cmd}")

#         process = await asyncio.create_subprocess_shell(
#             cmd, #*[sys.executable,'-c', cmd],
#             stdout=asyncio.subprocess.PIPE,
#             stderr=asyncio.subprocess.PIPE
#         ) #

#         # Read output line by line asynchronously
#         logger.debug(f"Started process (pid = {process.pid})")
#         while True:
#             line = await process.stdout.readline()
#             if not line:
#                 break
#             outs.append(line.decode().strip())
#             if any(line.decode().strip().startswith(keyword) for keyword in ["Starting standalone test","summary =","END "]):
#                 logger.info(line.decode().strip())
#             else:
#                 logger.debug(f"[jmeter-out] {line.decode().strip()}")

#         # Wait for the subprocess to exit
#         stdout, stderr = await process.communicate()
        
#         # Check the return code
#         if process.returncode == 0:
#             logger.debug(f"Process done with return code: {process.returncode}")

#             all_output = "\n".join(outs)
#             # parse the json
#             if "<statistics.json>" in all_output and "</statistics.json>" in all_output:
#                 statsJson = json.loads(all_output.split("<statistics.json>")[1].split("</statistics.json>")[0])

#                 logger.debug("Parsed statistics.json:")
#                 logger.debug(json.dumps(statsJson, indent=2))

#                 retvar = (statsJson, True, all_output)

#             # not done, still cleanup to do
#         else:
#             logger.debug(f"Process failed with return code: {process.returncode}")
#             if stderr:
#                 logger.debug(f"[STDERR] {stderr.decode().strip()}")
            
#             retvar = (None, False, f"Process failed with return code: {process.returncode}. Stderr: {stderr.decode().strip()}")
#             break

#     return retvar