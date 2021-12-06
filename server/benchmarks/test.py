import sys

sys.path.append("..")

import logging

from distributed_execution import DistributedExecution

if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)

    def square(v: int) -> int:
        return v * v

    values = list(range(1, 10001))

    with DistributedExecution() as d:
        results = d.map(square, values, chunk_size=100)

    print(results)
