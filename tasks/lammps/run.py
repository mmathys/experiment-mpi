import json
import re
import time
import requests
from invoke import task
from os import makedirs
from os.path import basename, join
from pprint import pprint
from multiprocessing import Pool

from hoststats.client import HostStats

from tasks.util.env import (
    RESULTS_DIR,
)
from tasks.util.faasm import (
    get_faasm_exec_time_from_json,
    get_faasm_worker_pods,
    get_faasm_invoke_host_port,
)
from tasks.util.openmpi import (
    NATIVE_HOSTFILE,
    get_native_mpi_namespace,
    get_native_mpi_pods,
    run_kubectl_cmd,
)
from tasks.lammps.env import (
    DOCKER_LAMMPS_BINARY,
    DOCKER_LAMMPS_DIR,
    LAMMPS_FAASM_USER,
    LAMMPS_FAASM_FUNC,
    get_faasm_benchmark,
)
from tasks.lammps.graph import plot_mpi_graph, plot_mpi_cross_host_msg

MESSAGE_TYPE_FLUSH = 3
FAASM_PID_FILE = "faasm_pids.txt"


def _init_csv_file(csv_name):
    result_dir = join(RESULTS_DIR, "lammps")
    makedirs(result_dir, exist_ok=True)

    result_file = join(result_dir, csv_name)
    makedirs(RESULTS_DIR, exist_ok=True)
    with open(result_file, "w") as out_file:
        out_file.write("WorldSize,Run,Reported,Actual\n")

    return result_file


def _process_lammps_result(
    lammps_output, result_file, num_procs, run_num, measured_time=None
):
    if "wasm" in result_file:
        result_json = json.loads(lammps_output, strict=False)
        print(
            "Processing lammps output: \n{}\n".format(
                result_json["output_data"]
            )
        )
        actual_time = get_faasm_exec_time_from_json(result_json)
        reported_time = re.findall(
            "Total wall time: ([0-9:]*)", result_json["output_data"]
        )
    else:
        actual_time = measured_time
        reported_time = re.findall("Total wall time: ([0-9:]*)", lammps_output)
        if measured_time == None:
            raise RuntimeError("Empty measured time for non-WASM execution")

    if len(reported_time) != 1:
        print(
            "Got {} matches for reported time, expected 1 from: \n{}".format(
                len(reported_time), lammps_output
            )
        )
        exit(1)

    reported_time = reported_time[0].split(":")
    reported_time = (
        int(reported_time[0]) * 3600
        + int(reported_time[1]) * 60
        + int(reported_time[2])
    )

    with open(result_file, "a") as out_file:
        out_file.write(
            "{},{},{},{:.2f}\n".format(
                num_procs, run_num, reported_time, actual_time
            )
        )


@task(iterable=["bench"])
def faasm(ctx, bench, repeats=1, nprocs=None, procrange=None, graph=False):
    """
    Run LAMMPS experiment on Faasm
    """
    # Sort out the number of processes to use during experiments
    if nprocs:
        num_procs = [nprocs]
    elif procrange:
        num_procs = range(1, int(procrange) + 1)
    else:
        num_procs = NUM_PROCS

    host, port = get_faasm_invoke_host_port()

    # pod_names = get_faasm_worker_pods()
    # stats = HostStats(
    #     pod_names,
    #     kubectl=True,
    #     kubectl_container="user-container",
    #     kubectl_ns="faasm",
    # )

    # Set up pid file
    with open(FAASM_PID_FILE, "a+") as f:
        f.write("========== BEGIN NEW EXP ==========\n")

    # Run multiple benchmarks if desired for convenience
    for b in bench:
        _bench = get_faasm_benchmark(b)

        result_file = _init_csv_file(
            "lammps_wasm_{}.csv".format(_bench["out_file"])
        )

        for np in num_procs:
            print("Running on Faasm with {} MPI processes".format(np))

            for run_num in range(repeats):
                # Url and headers for requests
                url = "http://{}:{}".format(host, port)

                # First, flush the host state
                print(
                    "Flushing functions, state, and shared files from workers"
                )
                msg = {"type": MESSAGE_TYPE_FLUSH}
                print("Posting to {} msg:".format(url))
                pprint(msg)
                response = requests.post(url, json=msg, timeout=None)
                if response.status_code != 200:
                    print(
                        "Flush request failed: {}:\n{}".format(
                            response.status_code, response.text
                        )
                    )
                print("Waiting for flush to propagate...")
                time.sleep(5)
                print("Done waiting")

                stats_csv = join(
                    RESULTS_DIR,
                    "lammps",
                    "hoststats_wasm_{}_{}_{}.csv".format(
                        _bench["out_file"], np, run_num
                    ),
                )

                start = time.time()
                # stats.start_collection()

                file_name = basename(_bench["data"][0])
                cmdline = "-in faasm://lammps-data/{}".format(file_name)
                msg = {
                    "user": LAMMPS_FAASM_USER,
                    "function": LAMMPS_FAASM_FUNC,
                    "cmdline": cmdline,
                    "mpi_world_size": int(np),
                    "async": True,
                    "record_exec_graph": graph,
                }
                print("Posting to {} msg:".format(url))
                pprint(msg)

                # Post asynch request
                response = requests.post(url, json=msg, timeout=None)
                # Get the async message id
                if response.status_code != 200:
                    print(
                        "Initial request failed: {}:\n{}".format(
                            response.status_code, response.text
                        )
                    )
                print("Response: {}".format(response.text))
                msg_id = int(response.text.strip())

                # Record message ids in a file
                with open(FAASM_PID_FILE, "a+") as f:
                    f.write("{}\n".format(msg_id))

                # Start polling for the result
                print("Polling message {}".format(msg_id))
                while True:
                    interval = 2
                    time.sleep(interval)

                    status_msg = {
                        "user": LAMMPS_FAASM_USER,
                        "function": LAMMPS_FAASM_FUNC,
                        "status": True,
                        "id": msg_id,
                    }
                    response = requests.post(url, json=status_msg)

                    if response.text.startswith("RUNNING"):
                        continue
                    elif response.text.startswith("FAILED"):
                        raise RuntimeError("Call failed")
                    elif not response.text:
                        raise RuntimeError("Empty status response")
                    else:
                        # If we reach this point it means the call has succeeded
                        break

                # stats.stop_and_write_to_csv(stats_csv)

                _process_lammps_result(response.text, result_file, np, run_num)

                if graph:
                    exec_graph(ctx, msg_id, xhost=True)

        print("Results written to {}".format(result_file))

    # Set up pid file
    with open(FAASM_PID_FILE, "a+") as f:
        f.write("===================================\n")


def send_request(url, msg):
    print("Sending request")
    response = requests.post(url, json=msg, timeout=None)
    # Get the async message id
    if response.status_code != 200:
        print(
            "Initial request failed: {}:\n{}".format(
                response.status_code, response.text
            )
        )
    print("Response: {}".format(response.text))
    msg_id = int(response.text.strip())

    # Record message ids in a file
    with open(FAASM_PID_FILE, "a+") as f:
        f.write("{}\n".format(msg_id))

    # Start polling for the result
    print("Polling message {}".format(msg_id))
    while True:
        interval = 2
        time.sleep(interval)

        status_msg = {
            "user": LAMMPS_FAASM_USER,
            "function": LAMMPS_FAASM_FUNC,
            "status": True,
            "id": msg_id,
        }
        response = requests.post(url, json=status_msg)

        if response.text.startswith("RUNNING"):
            continue
        elif response.text.startswith("FAILED"):
            raise RuntimeError("Call failed")
        elif not response.text:
            raise RuntimeError("Empty status response")
        else:
            # If we reach this point it means the call has succeeded
            break

    # _process_lammps_result(response.text, result_file, np, 1)

    print("Results written to {}".format(result_file))

    # Set up pid file
    with open(FAASM_PID_FILE, "a+") as f:
        f.write("===================================\n")
    return True


@task
def race(ctx):
    """
    Run race experiment
    """
    host, port = get_faasm_invoke_host_port()

    # Set up pid file
    with open(FAASM_PID_FILE, "a+") as f:
        f.write("========== BEGIN NEW EXP ==========\n")

    bench = "compute"
    b = bench

    # Run multiple benchmarks if desired for convenience
    _bench = get_faasm_benchmark(b)

    result_file = _init_csv_file(
        "race_lammps_wasm_{}.csv".format(_bench["out_file"])
    )

    # num processes
    np = 16
    print("Running on Faasm with {} MPI processes".format(np))

    # Url and headers for requests
    url = "http://{}:{}".format(host, port)

    # First, flush the host state
    print("Flushing functions, state, and shared files from workers")
    msg = {"type": MESSAGE_TYPE_FLUSH}
    print("Posting to {} msg:".format(url))
    pprint(msg)

    response = requests.post(url, json=msg, timeout=None)
    if response.status_code != 200:
        print(
            "Flush request failed: {}:\n{}".format(
                response.status_code, response.text
            )
        )
    print("Waiting for flush to propagate...")
    time.sleep(5)
    print("Done waiting")

    file_name = basename(_bench["data"][0])
    cmdline = "-in faasm://lammps-data/{}".format(file_name)
    msg = {
        "user": LAMMPS_FAASM_USER,
        "function": LAMMPS_FAASM_FUNC,
        "cmdline": cmdline,
        "mpi_world_size": int(np),
        "async": True,
    }
    print("Posting to {} msg:".format(url))
    pprint(msg)

    # set duplication factor for race condition
    num_dupl = 32
    print(f"Posting duplicate: {num_dupl}")

    res = []
    with Pool(num_dupl) as p:
        for i in range(num_dupl):
            p.apply_async(
                send_request, args=(url, msg), callback=lambda r: res.append(r)
            )
        p.close()
        p.join()

    print(res)
    print(f"Done posting {num_dupl} duplicates")


@task
def exec_graph(ctx, call_id, msg_type=-1, xhost=False):
    """
    Plot the execution graph for the given call ID
    """
    # Post request
    host, port = get_faasm_invoke_host_port()
    url = "http://{}:{}".format(host, port)
    msg = {
        "user": "",
        "function": "",
        "exec_graph": True,
        "id": int(call_id),
    }
    print("Posting to {} msg:".format(url))
    pprint(msg)

    # Get response
    response = requests.post(url, json=msg, timeout=None)
    if response.status_code != 200:
        print(
            "Exec graph request failed: {}:\n{}".format(
                response.status_code, response.text
            )
        )

    # Plot graph
    if xhost:
        plot_mpi_cross_host_msg(response.text)
    else:
        plot_mpi_graph(response.text, int(msg_type))


@task(iterable=["bench"])
def native(ctx, bench, repeats=1, nprocs=None, procrange=None):
    """
    Run LAMMPS experiment on OpenMPI
    """
    if nprocs:
        num_procs = [nprocs]
    elif procrange:
        num_procs = range(1, int(procrange) + 1)
    else:
        num_procs = NUM_PROCS

    namespace = get_native_mpi_namespace("lammps")
    pod_names, _ = get_native_mpi_pods("lammps")
    master_pod = pod_names[0]
    stats = HostStats(pod_names, kubectl=True, kubectl_ns=namespace)

    for b in bench:
        _bench = get_faasm_benchmark(b)

        result_file = _init_csv_file(
            "lammps_native_{}.csv".format(_bench["out_file"])
        )

        for np in num_procs:
            print("Running natively with {} MPI processes".format(np))
            print("Chosen pod {} as master".format(master_pod))

            for run_num in range(repeats):
                stats_csv = join(
                    RESULTS_DIR,
                    "lammps",
                    "hoststats_native_{}_{}_{}.csv".format(
                        _bench["out_file"], np, run_num
                    ),
                )

                start = time.time()
                stats.start_collection()

                native_cmdline = "-in {}/{}.faasm.native".format(
                    DOCKER_LAMMPS_DIR, _bench["data"][0]
                )
                mpirun_cmd = [
                    "mpirun",
                    "-np {}".format(np),
                    "-hostfile {}".format(NATIVE_HOSTFILE),
                    DOCKER_LAMMPS_BINARY,
                    native_cmdline,
                ]
                mpirun_cmd = " ".join(mpirun_cmd)

                exec_cmd = [
                    "exec",
                    master_pod,
                    "--",
                    "su mpirun -c '{}'".format(mpirun_cmd),
                ]
                exec_output = run_kubectl_cmd("lammps", " ".join(exec_cmd))
                print(exec_output)

                end = time.time()
                actual_time = end - start

                stats.stop_and_write_to_csv(stats_csv)

                _process_lammps_result(
                    exec_output, result_file, np, run_num, actual_time
                )
