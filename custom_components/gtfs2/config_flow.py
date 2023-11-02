"""ConfigFlow for GTFS integration."""
from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult
import homeassistant.helpers.config_validation as cv

from .const import DEFAULT_PATH, DOMAIN

from .gtfs_helper import (
    get_gtfs,
    get_next_departure,
    get_route_list,
    get_stop_list,
    get_datasources,
    remove_datasource,
)

_LOGGER = logging.getLogger(__name__)
STEP_SOURCE = vol.Schema(
    {
        vol.Required("file"): str,
        vol.Required("url", default="na"): str,
    }
)


@config_entries.HANDLERS.register(DOMAIN)
class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for GTFS."""

    VERSION = 1

    def __init__(self) -> None:
        """Init ConfigFlow."""
        self._pygtfs = ""
        self._data: dict[str, str] = {}
        self._user_inputs: dict = {}

    async def async_step_user(self, user_input: dict | None = None) -> FlowResult:
        """Handle the source."""
        errors: dict[str, str] = {}
        if user_input is None:
            datasources = get_datasources(self.hass, DEFAULT_PATH)
            datasources.append("setup new")
            datasources.append("remove datasource")
            return self.async_show_form(
                step_id="user",
                data_schema=vol.Schema(
                    {
                        vol.Required("file", default="setup new"): vol.In(datasources),
                    },
                ),
            )

        if user_input["file"] == "setup new":
            self._user_inputs.update(user_input)
            _LOGGER.debug(f"UserInputs File: {self._user_inputs}")
            return await self.async_step_source()
        elif user_input["file"] == "remove datasource":
            self._user_inputs.update(user_input)
            _LOGGER.debug(f"UserInputs File: {self._user_inputs}")
            return await self.async_step_remove()
        else:
            user_input["url"] = "na"
            self._user_inputs.update(user_input)
            _LOGGER.debug(f"UserInputs File: {self._user_inputs}")
            return await self.async_step_route()

    async def async_step_source(self, user_input: dict | None = None) -> FlowResult:
        """Handle a flow initialized by the user."""
        errors: dict[str, str] = {}
        if user_input is not None:
            check_data = await self._check_data(user_input)
            if check_data:
                errors["base"] = check_data
            else:
                self._user_inputs.update(user_input)
                _LOGGER.debug(f"UserInputs Data: {self._user_inputs}")
                return await self.async_step_route()

        return self.async_show_form(
            step_id="source",
            data_schema=vol.Schema(
                {
                    vol.Required("file"): str,
                    vol.Required("url"): str,
                },
            ),
            errors=errors,
        )

    async def async_step_remove(self, user_input: dict | None = None) -> FlowResult:
        """Handle a flow initialized by the user."""
        errors: dict[str, str] = {}
        if user_input is None:
            datasources = get_datasources(self.hass, DEFAULT_PATH)
            return self.async_show_form(
                step_id="remove",
                data_schema=vol.Schema(
                    {
                        vol.Required("file"): vol.In(datasources),
                    },
                ),
                errors=errors,
            )
        try:
            removed = remove_datasource(self.hass, DEFAULT_PATH, user_input["file"])
            _LOGGER.debug(f"removed value: {removed}")
        except Exception as ex:
            _LOGGER.info("Error while deleting : %s", {ex})
            return "generic_failure"
        return self.async_abort(reason="files_deleted")

    async def async_step_route(self, user_input: dict | None = None) -> FlowResult:
        """Handle the route."""
        self._pygtfs = get_gtfs(
            self.hass,
            DEFAULT_PATH,
            self._user_inputs["file"],
            self._user_inputs["url"],
            False,
        )
        errors: dict[str, str] = {}
        if user_input is None:
            return self.async_show_form(
                step_id="route",
                data_schema=vol.Schema(
                    {
                        vol.Required("route"): vol.In(get_route_list(self._pygtfs)),
                    },
                ),
            )
        self._user_inputs.update(user_input)
        _LOGGER.debug(f"UserInputs Route: {self._user_inputs}")
        return await self.async_step_stops()

    async def async_step_stops(self, user_input: dict | None = None) -> FlowResult:
        """Handle the route step."""
        errors: dict[str, str] = {}
        _LOGGER.debug(
            f"UserInputs RouteID: {self._user_inputs['route'].split(': ')[0]}"
        )
        if user_input is None:
            return self.async_show_form(
                step_id="stops",
                data_schema=vol.Schema(
                    {
                        vol.Required("origin"): vol.In(
                            get_stop_list(
                                self._pygtfs, self._user_inputs["route"].split(": ")[0]
                            )
                        ),
                        vol.Required("destination"): vol.In(
                            get_stop_list(
                                self._pygtfs, self._user_inputs["route"].split(": ")[0]
                            )
                        ),
                        vol.Required("name"): str,
                        vol.Optional("offset", default=0): int,
                        vol.Required("refresh_interval", default=15): int,
                        vol.Required("include_tomorrow"): vol.In(
                            {"no": "No", "yes": "Yes"}
                        ),
                    },
                ),
                errors=errors,
            )
        self._user_inputs.update(user_input)
        _LOGGER.debug(f"UserInputs Stops: {self._user_inputs}")
        check_config = await self._check_config(self._user_inputs)
        if check_config:
            _LOGGER.debug(f"CheckConfig: {check_config}")
            errors["base"] = check_config
        else:
            return self.async_create_entry(
                title=user_input["name"], data=self._user_inputs
            )

    async def _check_data(self, data):
        self._pygtfs = await self.hass.async_add_executor_job(
            get_gtfs, self.hass, DEFAULT_PATH, data["file"], data["url"], False
        )
        if self._pygtfs == "no_data_file":
            return "no_data_file"
        return None

    async def _check_config(self, data):
        self._pygtfs = await self.hass.async_add_executor_job(
            get_gtfs, self.hass, DEFAULT_PATH, data["file"], data["url"], False
        )
        if self._pygtfs == "no_data_file":
            return "no_data_file"
        self._data = {
            "schedule": self._pygtfs,
            "origin": data["origin"].split(": ")[0],
            "destination": data["destination"].split(": ")[0],
            "offset": data["offset"],
            "include_tomorrow": data["include_tomorrow"],
            "gtfs_dir": DEFAULT_PATH,
            "name": data["name"],
            "next_departure": None,
        }
        try:
            self._data["next_departure"] = await self.hass.async_add_executor_job(
                get_next_departure, self._data
            )
        except Exception as ex:  # pylint: disable=broad-except
            _LOGGER.info(
                "Config: error getting gtfs data from generic helper: %s",
                {ex},
                exc_info=1,
            )
            return "generic_failure"
        if self._data["next_departure"]:
            return None
        return "stop_incorrect"
