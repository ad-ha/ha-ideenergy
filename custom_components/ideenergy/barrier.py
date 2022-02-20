import asyncio
import enum
import random
from datetime import timedelta

from homeassistant.util import dt as dt_util

from . import _LOGGER
import ideenergy

ATTR_DELAY_INTERVAL = "delay_interval"
ATTR_MAX_AGE = "max_age"
ATTR_MAX_RETRIES = "max_retries"
ATTR_UPDATE_WINDOW_INTERVAL = "update_window_interval"
ATTR_COOLDOWN = "cooldown"
ATTR_FORCED = "forced"
ATTR_LAST_SUCCESS = "last_success"
ATTR_STATE = "state"
ATTR_RETRY = "retry"


class State(enum.Enum):
    FORCED = enum.auto()
    COOLDOWN_BARRIER_ACTIVE = enum.auto()
    RETRYING = enum.auto()
    TOO_RECENT = enum.auto()
    UPDATE_WINDOW_CLOSED = enum.auto()
    READY = enum.auto()


class Phase(enum.Enum):
    LOGIN = enum.auto()
    SELECT_CONTRACT = enum.auto()
    REQUEST_MEASURE = enum.auto()
    REQUEST_HISTORICAL = enum.auto()


class DataSource:
    def __init__(self, api, barrier, logger=None):
        self.api = api
        self._barrier = barrier
        self._logger = logger or _LOGGER.getChild("data-source")
        self._data = {"accumulated": None, "historical": []}

    @property
    def data(self):
        return self._data.copy()

    async def update(self):
        if self._barrier.allowed():
            try:
                phase = Phase.LOGIN
                await self._api.login()

                phase = Phase.SELECT_CONTRACT
                await self._api.select_contract(self._contact)

                phase = Phase.REQUEST_MEASURE
                self._state["accumulated"] = await self._api.get_measure().accumulated

                self._barrier.sucess()

            except ideenergy.ClientError as e:
                self._logger.debug(f"Error in phase '{phase}': {e}")
                self._barrier.fail()

        await self._barrier.delay()


class Barrier:
    def __init__(
        self,
        update_window_start_minute,
        update_window_end_minute,
        max_retries,
        max_age,
        delay_min_seconds,
        delay_max_seconds,
        logger=None,
    ):
        self._logger = logger or _LOGGER.getChild("barrier")

        self._max_age = max_age
        self._update_window_start_minute = update_window_start_minute
        self._update_window_end_minute = update_window_end_minute
        self._max_retries = max_retries
        self._delay_min_seconds = delay_min_seconds
        self._delay_max_seconds = delay_max_seconds

        zero_dt = dt_util.utc_from_timestamp(0)

        # state
        self._force_next = False
        self._failures = 0
        self._last_success = zero_dt
        self._cooldown = zero_dt

    @property
    def state(self):
        return self.get_state()

    @property
    def attributes(self):
        ret = {
            # Configuration
            ATTR_DELAY_INTERVAL: (self._delay_min_seconds, self._delay_max_seconds),
            ATTR_MAX_AGE: self._max_age,
            ATTR_MAX_RETRIES: self._max_retries,
            ATTR_UPDATE_WINDOW_INTERVAL: (
                self._update_window_start_minute,
                self._update_window_end_minute,
            ),
            # Internal state
            ATTR_COOLDOWN: self._cooldown,
            ATTR_FORCED: self._force_next,
            ATTR_LAST_SUCCESS: self._last_success,
            ATTR_STATE: self.state.name,
            ATTR_RETRY: self._failures,
        }
        # import json
        # import ipdb

        # ipdb.set_trace()
        # pass
        return ret

    def get_state(self, now=None):
        now = dt_util.as_utc(now or dt_util.utcnow())

        update_window_is_open = (
            self._update_window_start_minute
            <= dt_util.as_local(now).minute
            <= self._update_window_end_minute
        )
        last_success_age = (now - self._last_success).total_seconds()
        min_age = (
            self._update_window_end_minute - self._update_window_start_minute
        ) * 60

        # Check if cooldown has been reached
        if self._failures >= self._max_retries and now >= self._cooldown:
            self._logger.debug("cooldown barrier reached, resetting failures")
            self._failures = 0

        if self._force_next:
            self._logger.debug("Execution allowed: forced")
            return State.FORCED

        if now < self._cooldown:
            self._logger.debug(
                "Execution denied: cooldown barrier is active "
                f"({dt_util.as_local(self._cooldown)})"
            )
            return State.COOLDOWN_BARRIER_ACTIVE

        if self._failures > 0 and self._failures < self._max_retries:
            self._logger.debug("Execution allowed: retrying")
            return State.RETRYING

        if not update_window_is_open:
            self._logger.debug("Execution denied: update window is closed")
            return State.UPDATE_WINDOW_CLOSED

        if last_success_age <= min_age:
            self._logger.debug(
                "Execution denied: last success is too recent "
                f"({last_success_age} seconds, min: {min_age} seconds)"
            )
            return State.TOO_RECENT

        self._logger.debug("Execution allowed: no blockers")
        return State.READY

    def force_next(self):
        self._force_next = True

    def sucess(self, now=None):
        now = dt_util.as_utc(now or dt_util.utcnow())

        self._force_next = False
        self._failures = 0
        self._last_success = now

        self._logger.debug("Success registered")

    def fail(self, now=None):
        now = dt_util.as_utc(now or dt_util.utcnow())

        self._failures = self._failures + 1
        self._logger.debug(f"Fail registered ({self._failures}/{self._max_retries})")

        if self._failures >= self._max_retries:
            self._force_next = False
            self._cooldown = now + timedelta(seconds=self._max_age / 2)

            self._logger.debug(
                "Max failures reached, setup cooldown barrier until "
                f"{dt_util.as_local(self._cooldown)}"
            )

    def allowed(self, now=None):
        return self.get_state(now) in (State.FORCED, State.RETRYING, State.READY)

    async def delay(self):
        delay = (
            random.randint(self._delay_min_seconds * 10, self._delay_max_seconds * 10)
            / 10
        )
        self._logger.debug(f"Random delay: {delay} seconds")
        await asyncio.sleep(delay)
