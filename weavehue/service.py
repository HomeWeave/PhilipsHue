from threading import Event

from weavelib.http import AppHTTPServer
from weavelib.services import BasePlugin


class HueService(BasePlugin):
    def __init__(self, token, config):
        super().__init__(token)
        self.shutdown = Event()
        self.http = AppHTTPServer(self)

    def on_service_start(self, *args, **kwargs):
        super().on_service_start(*args, **kwargs)
        # self.rpc.start()
        self.http.register_folder("static", watch=True)
        self.notify_start()
        self.shutdown.wait()

    def on_service_stop(self):
        self.shutdown.set()
        self.http.stop()
        # self.rpc.stop()
        super().on_service_stop()


