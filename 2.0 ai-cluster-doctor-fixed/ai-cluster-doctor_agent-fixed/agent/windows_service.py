"""
windows_service.py
Wraps AgentRunner as a native Windows Service, so the Agent starts
automatically with Windows and runs silently in the background with no
user logged in.

Requires (Windows only): pip install pywin32

Install (run terminal as Administrator):
    python windows_service.py install
    python windows_service.py start

Set to start automatically on boot (either via services.msc -> Startup
type: Automatic, or):
    python windows_service.py --startup auto install

Uninstall:
    python windows_service.py stop
    python windows_service.py remove
"""

import logging
import threading

import servicemanager
import win32event
import win32service
import win32serviceutil

from agent_core import AgentRunner

logger = logging.getLogger("cluster_doctor_agent")


class ClusterDoctorAgentService(win32serviceutil.ServiceFramework):
    _svc_name_ = "ClusterDoctorAgent"
    _svc_display_name_ = "AI Cluster Doctor Agent"
    _svc_description_ = "Reports this PC's telemetry to the AI Cluster Doctor Host."

    def __init__(self, args):
        super().__init__(args)
        self.stop_event = win32event.CreateEvent(None, 0, 0, None)
        # AgentRunner() is deliberately NOT constructed here. It touches disk
        # (device_id persistence) - if that ever failed here, before the
        # service reports itself as running, Windows would just show a
        # generic "service did not start" error with no useful detail.
        # It's built in SvcDoRun instead, inside a try/except that logs a
        # clear message to the Windows Event Log.
        self.runner = None

    def SvcStop(self):
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        if self.runner is not None:
            self.runner.stop()
        win32event.SetEvent(self.stop_event)

    def _run_and_log_errors(self):
        """Thread target for the Agent loop. run_forever() already retries
        indefinitely on its own, so reaching this except block means it
        exited unexpectedly - log that loudly to the Event Log instead of
        letting a daemon thread die silently with no visible trace."""
        try:
            self.runner.run_forever()
        except Exception as exc:
            servicemanager.LogErrorMsg(
                f"AI Cluster Doctor Agent stopped unexpectedly: {exc}"
            )
            logger.error("Agent loop exited unexpectedly: %s", exc, exc_info=True)

    def SvcDoRun(self):
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
        try:
            self.runner = AgentRunner()
        except Exception as exc:
            # Only truly fatal init errors land here now (get_or_create_device_id
            # already falls back gracefully rather than raising). Report it
            # clearly and stop cleanly instead of the service silently
            # "failing to start" with no explanation.
            servicemanager.LogErrorMsg(
                f"AI Cluster Doctor Agent failed to start: {exc}"
            )
            self.ReportServiceStatus(win32service.SERVICE_STOPPED)
            return

        servicemanager.LogMsg(
            servicemanager.EVENTLOG_INFORMATION_TYPE,
            servicemanager.PYS_SERVICE_STARTED,
            (self._svc_name_, ""),
        )
        thread = threading.Thread(target=self._run_and_log_errors, daemon=True)
        thread.start()
        win32event.WaitForSingleObject(self.stop_event, win32event.INFINITE)


if __name__ == "__main__":
    win32serviceutil.HandleCommandLine(ClusterDoctorAgentService)
