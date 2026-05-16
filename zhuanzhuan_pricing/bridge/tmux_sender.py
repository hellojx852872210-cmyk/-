from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass


class TmuxError(RuntimeError):
    pass


@dataclass
class TmuxSender:
    target_pane: str

    def _run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        if shutil.which("tmux") is None:
            raise TmuxError("tmux 未安装或不在 PATH 中")
        return subprocess.run(args, capture_output=True, text=True)

    def validate_target(self) -> None:
        result = self._run(["tmux", "display-message", "-p", "-t", self.target_pane, "#{pane_id}"])
        if result.returncode != 0 or not result.stdout.strip():
            stderr = (result.stderr or "").strip()
            raise TmuxError(f"tmux pane 不存在: {self.target_pane}{': ' + stderr if stderr else ''}")

    def send_text(self, text: str, send_enter: bool = False) -> None:
        self.validate_target()
        result = self._run(["tmux", "send-keys", "-t", self.target_pane, "-l", text])
        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            raise TmuxError(f"发送到 tmux pane 失败: {stderr or self.target_pane}")
        if send_enter:
            enter_result = self._run(["tmux", "send-keys", "-t", self.target_pane, "Enter"])
            if enter_result.returncode != 0:
                stderr = (enter_result.stderr or "").strip()
                raise TmuxError(f"发送回车失败: {stderr or self.target_pane}")
