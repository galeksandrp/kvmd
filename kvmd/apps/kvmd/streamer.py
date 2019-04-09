# ========================================================================== #
#                                                                            #
#    KVMD - The main Pi-KVM daemon.                                          #
#                                                                            #
#    Copyright (C) 2018  Maxim Devaev <mdevaev@gmail.com>                    #
#                                                                            #
#    This program is free software: you can redistribute it and/or modify    #
#    it under the terms of the GNU General Public License as published by    #
#    the Free Software Foundation, either version 3 of the License, or       #
#    (at your option) any later version.                                     #
#                                                                            #
#    This program is distributed in the hope that it will be useful,         #
#    but WITHOUT ANY WARRANTY; without even the implied warranty of          #
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the           #
#    GNU General Public License for more details.                            #
#                                                                            #
#    You should have received a copy of the GNU General Public License       #
#    along with this program.  If not, see <https://www.gnu.org/licenses/>.  #
#                                                                            #
# ========================================================================== #


import os
import signal
import asyncio
import asyncio.subprocess

from typing import List
from typing import Dict
from typing import AsyncGenerator
from typing import Optional

import aiohttp

from ...logging import get_logger

from ... import gpio

from ... import __version__


# =====
class Streamer:  # pylint: disable=too-many-instance-attributes
    def __init__(  # pylint: disable=too-many-arguments,too-many-locals
        self,
        cap_pin: int,
        conv_pin: int,

        sync_delay: float,
        init_delay: float,
        init_restart_after: float,
        shutdown_delay: float,
        state_poll: float,

        quality: int,
        desired_fps: int,

        host: str,
        port: int,
        unix_path: str,
        timeout: float,

        cmd: List[str],

        loop: asyncio.AbstractEventLoop,
    ) -> None:

        self.__cap_pin = (gpio.set_output(cap_pin) if cap_pin >= 0 else -1)
        self.__conv_pin = (gpio.set_output(conv_pin) if conv_pin >= 0 else -1)

        self.__sync_delay = sync_delay
        self.__init_delay = init_delay
        self.__init_restart_after = init_restart_after
        self.shutdown_delay = shutdown_delay
        self.__state_poll = state_poll

        self.__params = {
            "quality": quality,
            "desired_fps": desired_fps,
        }

        assert port or unix_path
        self.__host = host
        self.__port = port
        self.__unix_path = unix_path
        self.__timeout = timeout

        self.__cmd = cmd

        self.__loop = loop

        self.__streamer_task: Optional[asyncio.Task] = None

        self.__http_session: Optional[aiohttp.ClientSession] = None

    async def start(self, params: Dict, no_init_restart: bool=False) -> None:
        logger = get_logger()
        logger.info("Starting streamer ...")

        self.__params = {key: params[key] for key in self.__params}  # Only known params
        assert 1 <= self.__params["quality"] <= 100
        assert 0 <= self.__params["desired_fps"] <= 30

        await self.__inner_start()
        if self.__init_restart_after > 0.0 and not no_init_restart:
            logger.info("Stopping streamer to restart ...")
            await self.__inner_stop()
            logger.info("Starting again ...")
            await self.__inner_start()

    async def stop(self) -> None:
        get_logger().info("Stopping streamer ...")
        await self.__inner_stop()

    def is_running(self) -> bool:
        return bool(self.__streamer_task)

    def get_params(self) -> Dict:
        return dict(self.__params)

    async def get_state(self) -> Dict:
        session = self.__ensure_session()
        state = None
        try:
            async with session.get(
                url="http://%s:%d/state" % (self.__host, self.__port),
                headers={"User-Agent": "KVMD/%s" % (__version__)},
                timeout=self.__timeout,
            ) as response:
                response.raise_for_status()
                state = (await response.json())["result"]
        except (aiohttp.ClientConnectionError, aiohttp.ServerConnectionError):
            pass
        except Exception:
            get_logger().exception("Invalid streamer response from /state")
        return {
            "params": self.get_params(),
            "state": state,
        }

    async def poll_state(self) -> AsyncGenerator[Dict, None]:
        while True:
            yield (await self.get_state())
            await asyncio.sleep(self.__state_poll)

    def get_app(self) -> str:
        return os.path.basename(self.__cmd[0])

    async def get_version(self) -> str:
        proc = await asyncio.create_subprocess_exec(
            *[self.__cmd[0], "--version"],
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            preexec_fn=(lambda: signal.signal(signal.SIGINT, signal.SIG_IGN)),
        )
        (stdout, _) = await proc.communicate()
        return stdout.decode(errors="ignore").strip()

    async def cleanup(self) -> None:
        if self.is_running():
            await self.stop()
        if self.__http_session:
            await self.__http_session.close()
            self.__http_session = None

    def __ensure_session(self) -> aiohttp.ClientSession:
        if not self.__http_session:
            if self.__unix_path:
                self.__http_session = aiohttp.ClientSession(connector=aiohttp.UnixConnector(path=self.__unix_path))
            else:
                self.__http_session = aiohttp.ClientSession()
        return self.__http_session

    async def __inner_start(self) -> None:
        assert not self.__streamer_task
        await self.__set_hw_enabled(True)
        self.__streamer_task = self.__loop.create_task(self.__run_streamer())

    async def __inner_stop(self) -> None:
        assert self.__streamer_task
        self.__streamer_task.cancel()
        await asyncio.gather(self.__streamer_task, return_exceptions=True)
        await self.__set_hw_enabled(False)
        self.__streamer_task = None

    async def __set_hw_enabled(self, enabled: bool) -> None:
        # XXX: This sequence is very important to enable converter and cap board
        if self.__cap_pin >= 0:
            gpio.write(self.__cap_pin, enabled)
        if self.__conv_pin >= 0:
            if enabled:
                await asyncio.sleep(self.__sync_delay)
            gpio.write(self.__conv_pin, enabled)
        if enabled:
            await asyncio.sleep(self.__init_delay)

    async def __run_streamer(self) -> None:  # pylint: disable=too-many-branches
        logger = get_logger(0)

        while True:  # pylint: disable=too-many-nested-blocks
            proc: Optional[asyncio.subprocess.Process] = None  # pylint: disable=no-member
            try:
                cmd = self.__make_cmd()
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    preexec_fn=(lambda: signal.signal(signal.SIGINT, signal.SIG_IGN)),
                )
                logger.info("Started streamer pid=%d: %s", proc.pid, cmd)

                empty = 0
                async for line_bytes in proc.stdout:  # type: ignore
                    line = line_bytes.decode(errors="ignore").strip()
                    if line:
                        logger.info("Streamer: %s", line)
                        empty = 0
                    else:
                        empty += 1
                        if empty == 100:  # asyncio bug
                            raise RuntimeError("Streamer/asyncio: too many empty lines")

                raise RuntimeError("Streamer unexpectedly died")

            except asyncio.CancelledError:
                break

            except Exception as err:
                if proc:
                    logger.exception("Unexpected streamer error: pid=%d", proc.pid)
                else:
                    logger.exception("Can't start streamer: %s", err)
                await asyncio.sleep(1)

            finally:
                if proc and proc.returncode is None:
                    await self.__kill(proc)

    def __make_cmd(self) -> List[str]:
        return [
            part.format(
                host=self.__host,
                port=self.__port,
                unix=self.__unix_path,
                **self.__params,
            )
            for part in self.__cmd
        ]

    async def __kill(self, proc: asyncio.subprocess.Process) -> None:  # pylint: disable=no-member
        try:
            proc.terminate()
            await asyncio.sleep(1)
            if proc.returncode is None:
                try:
                    proc.kill()
                except Exception:
                    if proc.returncode is not None:
                        raise
            await proc.wait()
            get_logger().info("Streamer killed: pid=%d; retcode=%d", proc.pid, proc.returncode)
        except Exception:
            if proc.returncode is None:
                get_logger().exception("Can't kill streamer pid=%d", proc.pid)
            else:
                get_logger().info("Streamer killed: pid=%d; retcode=%d", proc.pid, proc.returncode)
