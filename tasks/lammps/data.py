from invoke import task
from os.path import exists, join
import requests

from tasks.lammps.env import (
    BENCHMARKS,
    LAMMPS_DIR,
    LAMMPS_FAASM_DATA_PREFIX,
)
from tasks.util.faasm import get_faasm_upload_host_port


@task(default=True)
def upload(ctx, bench):
    """
    Upload LAMMPS benchmark data to Faasm
    """
    if bench not in BENCHMARKS:
        print("Unrecognized benchmark: {}".format(bench))
        print(
            "The supported LAMMPS benchmarks are: {}".format(BENCHMARKS.keys())
        )

    _bench = BENCHMARKS[bench]

    host, port = get_faasm_upload_host_port()
    url = "http://{}:{}/file".format(host, port)

    # Upload all data corresponding to the benchmark
    for data in _bench["data"]:
        file_name = data.split("/")[-1]
        host_path = join(LAMMPS_DIR, data + ".faasm")
        faasm_path = join(LAMMPS_FAASM_DATA_PREFIX, file_name)

        if not exists(host_path):
            print("Did not find data at {}".format(host_path))
            exit(1)

        print(
            "Uploading LAMMPS data ({}) to {} ({})".format(
                host_path, url, faasm_path
            )
        )
        response = requests.put(
            url,
            data=open(host_path, "rb"),
            headers={"FilePath": faasm_path},
        )

        print("Response {}: {}".format(response.status_code, response.text))
