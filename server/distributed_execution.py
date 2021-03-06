import base64
import json
import logging
import multiprocessing
import pathlib
import pickle
from dataclasses import dataclass
from functools import lru_cache
from threading import Thread
from time import sleep, time
from typing import Any, Callable, Iterable, List, Tuple

import cloudpickle
import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from tqdm import tqdm as tqdm_regular
from tqdm.notebook import tqdm_notebook
from websocket_server import WebsocketServer

logger = logging.getLogger("DistributedExecution")


def is_in_notebook() -> bool:
    try:
        get_ipython()
        return True
    except NameError:
        return False


tqdm = tqdm_notebook if is_in_notebook() else tqdm_regular


@dataclass
class ClientTask:
    client: Any
    task: Tuple[int, Any]
    time_to_live: int


def run_fastapi(packages: List[str], server_port: int):
    app = FastAPI()
    app.add_middleware(CORSMiddleware, allow_origins=["*"])

    @app.get("/packages")
    def get_packages():
        return packages

    @app.get("/")
    async def redirect():
        return RedirectResponse(url="/index.html")

    app.mount(
        "/",
        StaticFiles(directory=pathlib.Path(__file__).parent.resolve() / "frontend"),
        name="static",
    )

    uvicorn.run(app=app, host="0.0.0.0", port=server_port)


class DistributedExecution:
    def __init__(
        self,
        websocket_port=7700,
        server_port=7701,
        packages: List[str] = [],
        timeout_in_seconds=60,
    ):
        self._timeout_in_seconds = timeout_in_seconds
        self._server = WebsocketServer(
            host="0.0.0.0", port=websocket_port, loglevel=logging.INFO
        )
        self._server.set_fn_new_client(self._on_new_client)
        self._server.set_fn_client_left(self._on_client_lost)
        self._server.set_fn_message_received(self._on_message)

        self._server_port = server_port
        self._packages = packages

        self._clients_ready = []
        self._client_tasks: List[ClientTask] = []
        self._tasks: List[Tuple[int, Any]] = []
        self._map_function = None
        self._progress = None
        self._completed: List[Tuple[int, Any]] = []
        self._is_active = False

        logger.info(f"Created")

    def _start_webserver(self):
        self._webserver_process = multiprocessing.Process(
            target=run_fastapi,
            args=(self._packages, self._server_port)
        )
        self._webserver_process.start()
        logger.info(f"Web server started on http://0.0.0.0:{self._server_port}")

    def map(
        self,
        function: Callable[[Any], Any],
        values: Iterable[Any],
        chunk_size=1,
    ) -> List[Any]:
        values = list(enumerate(values))

        def map_function(v):
            i, d = v
            return i, function(d)

        self._map_function = map_function

        last_time = time()
        websocket_thread = Thread(target=self._server.serve_forever)
        websocket_thread.start()
        logger.info(
            f"WebSocket server started on ws://{self._server.host}:{self._server.port}"
        )

        self._start_webserver()

        self._is_active = True
        self._server.send_message_to_all(self._serialize_function(function))
        self._progress = tqdm(total=len(values))
        while self._tasks or values or self._client_tasks:
            while len(self._tasks) >= chunk_size or (not values and self._tasks):
                while not self._clients_ready:
                    sleep(0)
                client = self._clients_ready.pop(0)
                for _ in range(min(chunk_size, len(self._tasks))):
                    task = self._tasks.pop(0)
                    try:
                        self._client_tasks.append(
                            ClientTask(
                                client=client,
                                task=task,
                                time_to_live=self._timeout_in_seconds,
                            )
                        )
                        self._server.send_message(client, self._serialize_data(task))
                    except BrokenPipeError:
                        pass

            current_time = time()
            delta = current_time - last_time
            if delta > 1:
                last_time = current_time
                for t in self._client_tasks:
                    t.time_to_live -= delta
                timed_out = [t for t in self._client_tasks if t.time_to_live < 0]
                for t in timed_out:
                    logger.warning(f"Task {t.task[0]} timed out, retrying")
                    self._client_tasks.remove(t)
                    values.append(t.task)

            sleep(0)

            while values and len(self._tasks) < chunk_size:
                self._tasks.append(values.pop(0))

        actual_completed = [d for _, d in sorted(self._completed)]
        self._server.shutdown_gracefully()
        websocket_thread.join()
        logger.info(f"WebSocket server stopped")

        self._webserver_process.terminate()
        logger.info(f"Web server stopped")

        self._clients_ready = []
        self._client_tasks = []
        self._tasks = []
        self._map_function = None
        self._progress = None
        self._completed = []
        self._is_active = False

        return actual_completed

    @staticmethod
    @lru_cache(maxsize=20)
    def _serialize_function(function: Callable[[Any], Any]) -> str:
        return json.dumps(
            {
                "type": "function",
                "value": base64.b64encode(cloudpickle.dumps(function)).decode("utf-8"),
            }
        )

    @staticmethod
    def _serialize_data(data: Any) -> str:
        return json.dumps(
            {
                "type": "data",
                "value": base64.b64encode(pickle.dumps(data)).decode("utf-8"),
            }
        )

    def _on_new_client(self, client, server):
        logger.info(f"WebSocket client joined: {client['address']}")
        if self._map_function:
            server.send_message(client, self._serialize_function(self._map_function))
        self._clients_ready.append(client)

    def _on_client_lost(self, client, server):
        logger.info(f"WebSocket client left: {client}")
        if client in self._clients_ready:
            self._clients_ready.remove(client)

    def _on_message(self, client, server, message):
        if not self._is_active:
            return

        message = json.loads(message)
        {
            "ready": lambda *_: self._clients_ready.append(client),
            "result": self._on_get_result,
        }[message["type"]](client, message["value"])

    def _on_get_result(self, client, result):
        decoded_result = pickle.loads(base64.b64decode(result))
        client_task = [t for t in self._client_tasks if t.task[0] == decoded_result[0]]
        if client_task:
            self._client_tasks.remove(client_task[0])
        else:
            return

        if not [t for t in self._client_tasks if t.client == client]:
            self._clients_ready.append(client)

        self._completed.append(decoded_result)
        self._progress.update(1)

    def __enter__(self):
        logger.info(f"Initiated")
        return self

    def __exit__(self, type, value, traceback):
        self._server.shutdown_gracefully()
        logger.info(f"Destroyed")
