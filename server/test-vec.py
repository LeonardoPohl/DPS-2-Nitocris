from distributed_execution import DistributedExecution
import logging
import numpy as np

SIZE = 4096
VECTOR_COUNT = 10000

if __name__ == '__main__':
    logging.basicConfig(level=logging.DEBUG)

    A = np.random.random((SIZE, SIZE))
    vectors = [
      np.random.random(SIZE) for i in range(VECTOR_COUNT)  
    ]

    def vector_multiplication(v: np.ndarray) -> np.ndarray:
        return np.dot(A, v)

    with DistributedExecution() as d:
        results = d.map(vector_multiplication, vectors)

    print(results)
