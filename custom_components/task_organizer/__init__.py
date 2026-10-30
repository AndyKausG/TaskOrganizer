"""Initialization of the TaskOrganizer integration."""

import copy
import logging
import random
import re
import time
import uuid
from datetime import timedelta
from datetime import date, datetime
import voluptuous as vol

from homeassistant.components import persistent_notification, websocket_api
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_change,
    async_track_time_interval,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    EVENT_TASK_UPDATED,
    EVENT_LEADERBOARD_CHANGED,
    EVENT_TASK_COMPLETED,
    EVENT_TASK_CREATED,
    EVENT_RESET,
    EVENT_BONUS_OPENED,
    EVENT_APPROVAL_REQUESTED,
    EVENT_APPROVAL_RESOLVED,
    EVENT_KID_BIRTHDAY,
    EVENT_MOBILE_APP_ACTION,
    ACTION_APPROVE,
    ACTION_NOT_DONE,
    ACTION_CHEAT,
    STORAGE_KEY,
    STORAGE_VERSION,
    WS_TYPE_ADD_TASK,
    WS_TYPE_COMPLETE_TASK,
    WS_TYPE_DELETE_HISTORY_ITEM,
    WS_TYPE_DELETE_TASK,
    WS_TYPE_EDIT_HISTORY_ITEM,
    WS_TYPE_EDIT_TASK,
    WS_TYPE_FACTORY_RESET,
    WS_TYPE_GET_DATA,
    WS_TYPE_IMPORT_TASKS,
    WS_TYPE_PAUSE_TASK,
    WS_TYPE_UPDATE_POINTS,
    WS_TYPE_UPDATE_SETTINGS,
    WS_TYPE_ADD_TEMPLATE,
    WS_TYPE_EDIT_TEMPLATE,
    WS_TYPE_DELETE_TEMPLATE,
    WS_TYPE_SET_KID_PROFILE,
    WS_TYPE_DELETE_KID_PROFILE,
    WS_TYPE_KID_VERIFY_PIN,
    WS_TYPE_KID_COMPLETE_TASK,
    WS_TYPE_KID_CLAIM_TASK,
    WS_TYPE_WIZARD_CATALOG,
    WS_TYPE_WIZARD_PREVIEW,
    WS_TYPE_WIZARD_APPLY,
    WS_TYPE_SET_BONUS_TRIGGERS,
    WS_TYPE_APPROVE,
    WS_TYPE_REJECT,
    WS_TYPE_ADJUST_POINTS,
    WS_TYPE_UNDO_COMPLETION,
    WS_TYPE_ADD_ADHOC_BONUS,
    SERVICE_ADJUST_POINTS,
    SERVICE_ADD_TASK,
    SERVICE_COMPLETE_TASK_BY_NAME,
    SERVICE_RESET_MONTHLY_POINTS,
    SERVICE_FACTORY_RESET,
    SERVICE_SET_TASK_DUE_BY_NAME,
    SERVICE_PAUSE_TASK_BY_NAME,
    SERVICE_REMOVE_TASK_BY_NAME,
    SERVICE_EDIT_TASK_BY_NAME,
    SERVICE_ADD_TEMPLATE,
    SERVICE_REMOVE_TEMPLATE_BY_NAME,
    SERVICE_EDIT_TEMPLATE_BY_NAME,
)
from . import kids_catalog
from .kids import (
    DEFAULT_CLAIM_MINUTES,
    ERR_NOT_FOUND,
    ERR_NOT_YET,
    ERR_TOO_LATE,
    MAX_ADJUST_POINTS,
    MAX_REASON_LENGTH,
    TYPE_BIRTHDAY,
    TYPE_BONUS,
    TYPE_MISSED,
    TYPE_PENALTY,
    TYPE_TASK,
    PinLimiter,
    birthday_bonus_points,
    birthday_duty_free,
    birthday_kids,
    build_kid_profile,
    check_eligibility,
    cheat_penalty,
    cheat_strict_days,
    clamp_adjustment,
    kid_age,
    needs_approval,
    parse_birthday,
    parse_dt,
    period_task_points,
    unlocked_by_age,
    is_open,
    is_valid_color,
    is_valid_pin,
    merge_imported_kids,
    migrate_task_like,
    normalize_category,
    normalize_min_age,
    normalize_time,
    is_expired,
    is_missed,
    next_due_day,
    restore_full_goals,
    participant_id,
    resolve_participant_name,
    sanitize_settings,
    verify_kid_pin,
)

# Global logger for the integration
_LOGGER = logging.getLogger(__name__)

# Supported platforms
PLATFORMS = ["sensor", "button"]

# Optional "HH:MM" time-window field (empty / null = no limit)
_TIME_FIELD = vol.Any(None, "", vol.Match(r"^([01]\d|2[0-3]):[0-5]\d$"))


async def update_listener(hass: HomeAssistant, entry: ConfigEntry):
    """Called when options are changed via the UI gear icon."""
    # Update the settings in the main data structure
    hass.data[DOMAIN]["data"]["settings"] = dict(entry.options)
    async_setup_bonus_triggers(hass)

    hass.bus.async_fire(EVENT_TASK_UPDATED)


def _calculate_points_per_user(total_points: float, user_count: int) -> float:
    """
    Calculate the fair share of points per user.
    
    :param total_points: The total complexity/points of the task.
    :param user_count: The amount of users that completed the task.
    :return: The rounded points per user.
    """
    if user_count <= 0:
        return 0.0
    return round(total_points / user_count, 1)


def _get_user_id(hass: HomeAssistant, target: str) -> str | None:
    """
    Resolve a user ID from a given string target (which might be an entity_id).
    
    :param hass: The Home Assistant instance.
    :param target: The input string (entity_id or user_id).
    :return: The resolved user ID or the initial string if not an entity.
    """
    if not target:
        return None
        
    if target.startswith("person."):
        state = hass.states.get(target)
        if state and state.attributes.get("user_id"):
            return state.attributes.get("user_id")
            
    return target


def _fire_leaderboard_event_if_changed(hass: HomeAssistant, old_points: dict, new_points: dict):
    """
    Compares the old and new points. Fires an event if the top 3 user order changes.
    """
    # Sort by points (descending) and then by user_id (ascending) for stable tie-breaking
    old_sorted = sorted(old_points.items(), key=lambda item: (-item[1], item[0]))
    new_sorted = sorted(new_points.items(), key=lambda item: (-item[1], item[0]))
    
    # Get top 3
    old_top_3 = [{"user_id": uid, "points": pts} for uid, pts in old_sorted[:3]]
    new_top_3 = [{"user_id": uid, "points": pts} for uid, pts in new_sorted[:3]]
    
    # Check if the order of user_ids changed in the top 3
    old_order = [item["user_id"] for item in old_top_3]
    new_order = [item["user_id"] for item in new_top_3]
    
    if old_order != new_order:
        # Resolve friendly names for a better event payload experience
        person_states = hass.states.async_all("person")
        for item in old_top_3 + new_top_3:
            item["name"] = resolve_participant_name(person_states, item["user_id"])
            
        hass.bus.async_fire(EVENT_LEADERBOARD_CHANGED, {
            "old_leaderboard": old_top_3,
            "new_leaderboard": new_top_3,
        })


@websocket_api.websocket_command({vol.Required("type"): WS_TYPE_GET_DATA})
@websocket_api.async_response
async def ws_get_data(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict):
    """
    Handle the websocket request to get all task organizer data.
    
    :param hass: The Home Assistant instance.
    :param connection: The active websocket connection.
    :param msg: The incoming message payload.
    """
    data = hass.data[DOMAIN]["data"]
    
    # Get options from ConfigEntry and inject as 'settings'
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    
    # Create a copy to avoid accidentally modifying the original dictionary
    response_data = dict(data)
    # Never expose PIN hashes/salts to the frontend (only "has_pin")
    response_data["settings"] = sanitize_settings(entry.options)
    response_data.setdefault("pending", {})
    response_data.setdefault("birthday_levelup", {})
    response_data["family_points"] = _family_points(hass)
    
    connection.send_result(msg["id"], response_data)


def _family_points(hass: HomeAssistant) -> float:
    """Approved task points of all kids in the current period (family goal).

    Computed from the history entries of type ``task`` only: the birthday
    bonus, manual bonuses and penalties do not count towards the family goal.
    """
    data = hass.data[DOMAIN]["data"]
    entries = hass.config_entries.async_entries(DOMAIN)
    kids = (entries[0].options.get("kids") or {}) if entries else {}
    return period_task_points(data.get("history", []), parse_dt(data.get("current_period_start")), kids)


def _undo_info(task: dict) -> dict:
    """What is needed to undo a completion of ``task``: taken BEFORE it advances.

    One-time tasks (interval 0) are deleted by a completion, so a compact
    snapshot of the whole task is kept to be able to recreate it.
    """
    info = {
        "previous_due_date": task.get("due_date"),
        "previous_paused_until": task.get("paused_until"),
        "subtasks": copy.deepcopy(task.get("subtasks") or []),
    }
    if task.get("interval", 7) == 0:
        snapshot = copy.deepcopy(task)
        snapshot.pop("claimed_by", None)
        snapshot.pop("claimed_until", None)
        info["task_snapshot"] = snapshot
    return info


def _advance_task(data: dict, task_id: str) -> dict | None:
    """Move a task to its next occurrence like a completion - without points or history.

    Recurring tasks get a new due date and fresh subtasks, one-time tasks
    (interval 0) are deleted. A kid's reservation is dropped.

    :return: the undo information (see ``_undo_info``) or ``None`` for an unknown task.
    """
    task = data["tasks"].get(task_id)
    if task is None:
        return None
    undo = _undo_info(task)
    task.pop("claimed_by", None)
    task.pop("claimed_until", None)

    interval = task.get("interval", 7)
    if interval > 0:
        task["due_date"] = (dt_util.utcnow() + timedelta(days=interval)).isoformat()
        task["paused_until"] = None

        # Reset subtasks for recurring tasks
        if "subtasks" in task:
            for st in task["subtasks"]:
                st["done"] = False

    # A one-time task is removed from the main dictionary
    if interval == 0:
        del data["tasks"][task_id]
    return undo


def _award_task_points(
    hass: HomeAssistant,
    data: dict,
    task_id: str,
    task_name: str | None,
    users_with_points: dict,
    total_points: float,
    points_per_user: float | None,
    undo: dict | None = None,
) -> None:
    """Add points + history entries of a completed task and fire the events.

    Shared by a normal completion and by the approval of a pending one, so
    both produce identical history entries and events. All entries of one
    completion share a ``completion_id`` (used to undo them together).
    """
    old_points = data["points"].copy()
    now = dt_util.utcnow().isoformat()
    completion_id = str(uuid.uuid4())

    for u_id, pts in users_with_points.items():
        data["points"].setdefault(u_id, 0.0)
        data["points"][u_id] += pts

        history_entry = {
            "id": str(uuid.uuid4()),
            "task_id": task_id,
            "task_name": task_name if task_name is not None else "Unknown task",
            "user_id": u_id,
            "points": pts,
            "timestamp": now,
            "type": TYPE_TASK,
            "completion_id": completion_id,
        }
        if undo:
            history_entry.update(copy.deepcopy(undo))
        data["history"].insert(0, history_entry)

    _fire_leaderboard_event_if_changed(hass, old_points, data["points"])
    hass.bus.async_fire(EVENT_TASK_COMPLETED, {
        "task_id": task_id,
        "task_name": task_name,
        "completed_by": list(users_with_points.keys()),
        "points_per_user": points_per_user,
        "total_points": total_points
    })


async def _async_complete_task_internal(hass: HomeAssistant, task_id: str, completed_by: list, points_distribution: dict = None):
    """
    Internal helper to process task completion logic, including points, history and recurring logic.
    
    :param hass: The Home Assistant instance.
    :param task_id: The unique ID of the task.
    :param completed_by: List of user IDs who completed the task.
    :param points_distribution: Mapping of user ID to percentage for point distribution.
    """
    data = hass.data[DOMAIN]["data"]
    if task_id not in data["tasks"]:
        return False

    task = data["tasks"][task_id]
    total_points = float(task.get("complexity", 1))

    # Create mapping for users receiving points (either from distribution or fair share)
    users_with_points = {}
    if points_distribution:
        for u_id, percentage in points_distribution.items():
            users_with_points[u_id] = round((percentage / 100) * total_points, 1)
    else:
        share = _calculate_points_per_user(total_points, len(completed_by))
        for u_id in completed_by:
            users_with_points[u_id] = share

    task_name = task.get("name")
    undo = _advance_task(data, task_id)
    _award_task_points(
        hass, data, task_id, task_name, users_with_points, total_points,
        None if points_distribution else _calculate_points_per_user(total_points, len(completed_by)),
        undo,
    )

    await hass.data[DOMAIN]["store"].async_save(data)
    hass.bus.async_fire(EVENT_TASK_UPDATED)
    return True


@websocket_api.websocket_command({
    vol.Required("type"): WS_TYPE_COMPLETE_TASK,
    vol.Required("task_id"): str,
    vol.Optional("completed_by"): cv.ensure_list,
    vol.Optional("points_distribution"): dict, # Allow points_distribution from frontend
})
@websocket_api.async_response
async def ws_complete_task(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict):
    """
    Mark a specific task as completed via websocket.
    """
    task_id = msg["task_id"]
    completed_by = msg.get("completed_by")
    
    if not completed_by:
        completed_by = [connection.user.id]

    success = await _async_complete_task_internal(hass, task_id, completed_by, msg.get("points_distribution"))

    if not success:
        connection.send_error(msg["id"], "not_found", "Task not found")
        return

    connection.send_result(msg["id"], {"success": True})


@websocket_api.websocket_command({
    vol.Required("type"): WS_TYPE_ADD_TASK,
    vol.Required("name"): str, 
    vol.Optional("description", default=""): str,
    vol.Optional("area", default=""): str,
    vol.Required("interval"): int,
    vol.Optional("assignees"): cv.ensure_list,
    vol.Required("complexity"): vol.All(int, vol.Range(min=1, max=10)),
    vol.Optional("category", default="basis"): str,
    vol.Optional("min_age", default=0): vol.All(int, vol.Range(min=0, max=18)),
    vol.Optional("requires_approval", default=False): bool,
    vol.Optional("requires_routine", default=False): bool,
    vol.Optional("available_from"): _TIME_FIELD,
    vol.Optional("available_until"): _TIME_FIELD,
    vol.Required("icon"): str,
    vol.Optional("custom_due_date"): vol.Any(str, None),
    vol.Optional("paused_until"): vol.Any(str, None),
    vol.Optional("override_overdue_days"): vol.Any(vol.All(int, vol.Range(min=0, max=9999)), None),
    vol.Optional("subtasks"): cv.ensure_list,
})
@websocket_api.async_response
async def ws_add_task(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict):
    """
    Create a new task via websocket.
    
    :param hass: The Home Assistant instance.
    :param connection: The active websocket connection.
    :param msg: The incoming message payload with task details.
    """
    data = hass.data[DOMAIN]["data"]
    task_id = str(uuid.uuid4())
    custom_due_date_str = msg.get("custom_due_date")
    if custom_due_date_str: # Ensure datetime is imported for fromisoformat
        due_date = datetime.fromisoformat(custom_due_date_str).isoformat() 
    else:
        due_date = dt_util.utcnow().isoformat() # Default if no custom date is provided
    
    new_task = {
        "id": task_id, 
        "name": msg["name"], 
        "description": msg.get("description", ""),
        "area": msg.get("area", ""),
        "interval": msg["interval"],
        "assignees": msg.get("assignees", []), 
        "complexity": msg["complexity"],
        "category": normalize_category(msg.get("category")),
        "min_age": normalize_min_age(msg.get("min_age", 0)),
        "requires_approval": bool(msg.get("requires_approval", False)),
        "requires_routine": bool(msg.get("requires_routine", False)),
        "available_from": normalize_time(msg.get("available_from")),
        "available_until": normalize_time(msg.get("available_until")),
        "icon": msg["icon"],
        "due_date": due_date, 
        "paused_until": msg.get("paused_until"),
        "override_overdue_days": msg.get("override_overdue_days"),
        "subtasks": msg.get("subtasks", [])
    }
    
    data["tasks"][task_id] = new_task
    hass.bus.async_fire(EVENT_TASK_CREATED, new_task)
    await hass.data[DOMAIN]["store"].async_save(data)
    hass.bus.async_fire(EVENT_TASK_UPDATED)
    connection.send_result(msg["id"], {"success": True, "task_id": task_id})


@websocket_api.websocket_command({
    vol.Required("type"): WS_TYPE_EDIT_TASK,
    vol.Required("task_id"): str, 
    vol.Optional("name"): str,
    vol.Optional("description"): str,
    vol.Optional("area"): str,
    vol.Optional("interval"): int, 
    vol.Optional("assignees"): cv.ensure_list,
    vol.Optional("complexity"): vol.All(int, vol.Range(min=1, max=10)),
    vol.Optional("category"): str, 
    vol.Optional("min_age"): vol.All(int, vol.Range(min=0, max=18)),
    vol.Optional("requires_approval"): bool,
    vol.Optional("requires_routine"): bool,
    vol.Optional("available_from"): _TIME_FIELD,
    vol.Optional("available_until"): _TIME_FIELD,
    vol.Optional("icon"): str,
    vol.Optional("custom_due_date"): vol.Any(str, None),
    vol.Optional("paused_until"): vol.Any(str, None),
    vol.Optional("override_overdue_days"): vol.Any(vol.All(int, vol.Range(min=0, max=9999)), None),
    vol.Optional("subtasks"): cv.ensure_list,
})
@websocket_api.async_response
async def ws_edit_task(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict):
    """
    Edit an existing task via websocket.
    
    :param hass: The Home Assistant instance.
    :param connection: The active websocket connection.
    :param msg: The incoming message payload with updated task details.
    """
    data = hass.data[DOMAIN]["data"]
    task_id = msg["task_id"]
    
    if task_id not in data["tasks"]:
        connection.send_error(msg["id"], "not_found", "Task not found")
        return
        
    task_ref = data["tasks"][task_id]
    keys_to_update = ["name", "description", "area", "interval", "assignees", "complexity", "icon", "subtasks"]
    
    for key in keys_to_update:
        if key in msg: 
            task_ref[key] = msg[key]

    if "category" in msg:
        task_ref["category"] = normalize_category(msg["category"])
    if "min_age" in msg:
        task_ref["min_age"] = normalize_min_age(msg["min_age"])
    if "requires_approval" in msg:
        task_ref["requires_approval"] = bool(msg["requires_approval"])
    if "requires_routine" in msg:
        task_ref["requires_routine"] = bool(msg["requires_routine"])
    for key in ("available_from", "available_until"):
        if key in msg:
            task_ref[key] = normalize_time(msg[key])

    if msg.get("custom_due_date"):
        task_ref["due_date"] = dt_util.as_utc(dt_util.parse_datetime(msg["custom_due_date"])).isoformat()
        
    if "paused_until" in msg:
        task_ref["paused_until"] = msg["paused_until"]

    if "override_overdue_days" in msg:
        task_ref["override_overdue_days"] = msg["override_overdue_days"]
        
    await hass.data[DOMAIN]["store"].async_save(data)
    hass.bus.async_fire(EVENT_TASK_UPDATED)
    connection.send_result(msg["id"], {"success": True})


@websocket_api.websocket_command({
    vol.Required("type"): WS_TYPE_DELETE_TASK,
    vol.Required("task_id"): str,
})
@websocket_api.async_response
async def ws_delete_task(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict):
    """
    Delete a task via websocket.
    
    :param hass: The Home Assistant instance.
    :param connection: The active websocket connection.
    :param msg: The incoming message payload with task_id.
    """
    data = hass.data[DOMAIN]["data"]
    task_id = msg["task_id"]
    
    if task_id in data["tasks"]:
        del data["tasks"][task_id]
        await hass.data[DOMAIN]["store"].async_save(data)
        hass.bus.async_fire(EVENT_TASK_UPDATED)
        connection.send_result(msg["id"], {"success": True})


@websocket_api.websocket_command({
    vol.Required("type"): WS_TYPE_FACTORY_RESET,
})
@websocket_api.async_response
async def ws_factory_reset(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict):
    """
    Perform a complete factory reset of the integration data.
    
    :param hass: The Home Assistant instance.
    :param connection: The active websocket connection.
    :param msg: The incoming message payload.
    """
    await hass.services.async_call(DOMAIN, SERVICE_FACTORY_RESET)
    connection.send_result(msg["id"], {"success": True})


# Keys that must never be written through update_settings / import
_PROTECTED_SETTING_KEYS = (
    "pending", "history", "points", "tasks", "templates",
    "birthday_handled", "birthday_levelup", "monthly_history",
)


@websocket_api.websocket_command({
    vol.Required("type"): WS_TYPE_UPDATE_SETTINGS,
    vol.Required("settings"): dict,
})
@websocket_api.async_response
async def ws_update_settings(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict):
    """Saves the settings from the card directly into the Home Assistant OptionsFlow."""
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    
    # Update the Entry-Options with the new settings from the card
    new_options = dict(entry.options)
    incoming = dict(msg["settings"])
    # Kid profiles (incl. PIN hashes) are only managed through the dedicated
    # admin commands set_kid_profile / delete_kid_profile.
    incoming.pop("kids", None)
    # Triggers and the wizard memory have their own admin-only commands
    incoming.pop("bonus_triggers", None)
    incoming.pop("wizard", None)
    # Runtime data (pending approvals, history, points, birthday memory) is not a setting
    for key in _PROTECTED_SETTING_KEYS:
        incoming.pop(key, None)
    new_options.update(incoming)
    
    hass.config_entries.async_update_entry(entry, options=new_options)
    # The update event is triggered automatically by the update_listener
    connection.send_result(msg["id"], {"success": True})


@websocket_api.websocket_command({
    vol.Required("type"): WS_TYPE_DELETE_HISTORY_ITEM,
    vol.Required("entry_id"): str,
})
@websocket_api.async_response
async def ws_delete_history_item(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict):
    data = hass.data[DOMAIN]["data"]
    entry_id = msg["entry_id"]
    
    history = data["history"]
    entry_to_delete = next((item for item in history if item["id"] == entry_id), None)
    
    if entry_to_delete:
        old_points = data["points"].copy()
        u_id = entry_to_delete["user_id"]
        pts = float(entry_to_delete.get("points", 0))
        
        if u_id in data["points"]:
            data["points"][u_id] = max(0.0, float(data["points"][u_id]) - pts)
            
        data["history"] = [item for item in history if item["id"] != entry_id]
        
        _fire_leaderboard_event_if_changed(hass, old_points, data["points"])
        
        await hass.data[DOMAIN]["store"].async_save(data)
        hass.bus.async_fire(EVENT_TASK_UPDATED)
    
    connection.send_result(msg["id"], {"success": True})


@websocket_api.websocket_command({
    vol.Required("type"): WS_TYPE_EDIT_HISTORY_ITEM,
    vol.Required("entry_id"): str,
    vol.Required("points"): vol.Coerce(float),
    vol.Required("user_id"): str,
})
@websocket_api.async_response
async def ws_edit_history_item(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict):
    data = hass.data[DOMAIN]["data"]
    entry_id = msg["entry_id"]
    new_points = msg["points"]
    new_user_id = msg["user_id"]
    
    history = data["history"]
    entry = next((item for item in history if item["id"] == entry_id), None)
    
    if entry:
        old_all_points = data["points"].copy()
        
        old_u_id = entry["user_id"]
        old_pts = float(entry.get("points", 0))
        
        if old_u_id in data["points"]:
            data["points"][old_u_id] = max(0.0, float(data["points"][old_u_id]) - old_pts)
            
        if new_user_id not in data["points"]:
            data["points"][new_user_id] = 0.0
        data["points"][new_user_id] = max(0.0, data["points"][new_user_id] + new_points)
        
        entry["user_id"] = new_user_id
        entry["points"] = new_points
        
        _fire_leaderboard_event_if_changed(hass, old_all_points, data["points"])
        
        await hass.data[DOMAIN]["store"].async_save(data)
        hass.bus.async_fire(EVENT_TASK_UPDATED)
        
    connection.send_result(msg["id"], {"success": True})


@websocket_api.websocket_command({
    vol.Required("type"): WS_TYPE_IMPORT_TASKS,
    vol.Optional("tasks"): dict,
    vol.Optional("templates"): dict,
    vol.Optional("history"): list,
    vol.Optional("points"): dict,
    vol.Optional("monthly_history"): dict,
    vol.Optional("current_month"): str,
    vol.Optional("current_period_start"): str,
    vol.Optional("settings"): dict,
})
@websocket_api.async_response
async def ws_import_tasks(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict):
    data = hass.data[DOMAIN]["data"]
    
    if "settings" in msg:
        incoming_settings = dict(msg["settings"])
        # Kids profiles are merged separately (admin only, never takes hashes)
        incoming_kids = incoming_settings.pop("kids", None)
        if not connection.user.is_admin:
            incoming_settings.pop("bonus_triggers", None)
            incoming_settings.pop("wizard", None)
        for key in _PROTECTED_SETTING_KEYS:
            incoming_settings.pop(key, None)
        for k, v in incoming_settings.items():
            data.setdefault("settings", {})[k] = v
        entries = hass.config_entries.async_entries(DOMAIN)
        if entries:
            entry = entries[0]
            new_options = dict(entry.options)
            new_options.update(incoming_settings)
            if incoming_kids is not None and connection.user.is_admin:
                new_options["kids"] = merge_imported_kids(entry.options.get("kids"), incoming_kids)
            hass.config_entries.async_update_entry(entry, options=new_options)

    if "tasks" in msg:
        imported = msg["tasks"]
        for tid, tdata in imported.items():
            if isinstance(tdata, dict):
                migrate_task_like(tdata)
            data["tasks"][tid] = tdata
            
    if "templates" in msg:
        imported_templates = msg["templates"]
        for tid, tdata in imported_templates.items():
            if isinstance(tdata, dict):
                migrate_task_like(tdata)
            data.setdefault("templates", {})[tid] = tdata
            
    if "history" in msg:
        existing_history_ids = {h["id"] for h in data.setdefault("history", [])}
        for h_entry in msg["history"]:
            if h_entry.get("id") not in existing_history_ids:
                data["history"].append(h_entry)
        data["history"].sort(key=lambda x: x.get("timestamp", ""), reverse=True)

    if "points" in msg:
        for uid, pts in msg["points"].items():
            data.setdefault("points", {})[uid] = pts

    if "monthly_history" in msg:
        for month, m_data in msg["monthly_history"].items():
            data.setdefault("monthly_history", {})[month] = m_data

    if "current_month" in msg:
        data["current_month"] = msg["current_month"]

    if "current_period_start" in msg:
        data["current_period_start"] = msg["current_period_start"]
            
    await hass.data[DOMAIN]["store"].async_save(data)
    hass.bus.async_fire(EVENT_TASK_UPDATED)
    connection.send_result(msg["id"], {"success": True})

@websocket_api.websocket_command({
    vol.Required("type"): WS_TYPE_ADD_TEMPLATE,
    vol.Required("name"): str,
    vol.Optional("description", default=""): str,
    vol.Optional("area", default=""): str,
    vol.Required("complexity"): vol.All(int, vol.Range(min=1, max=10)),
    vol.Required("icon"): str,
    vol.Optional("category", default="basis"): str,
    vol.Optional("min_age", default=0): vol.All(int, vol.Range(min=0, max=18)),
    vol.Optional("requires_approval", default=False): bool,
    vol.Optional("requires_routine", default=False): bool,
    vol.Optional("available_from"): _TIME_FIELD,
    vol.Optional("available_until"): _TIME_FIELD,
    vol.Optional("interval", default=7): int,
    vol.Optional("assignees", default=[]): cv.ensure_list,
    vol.Optional("subtasks", default=[]): cv.ensure_list,
})
@websocket_api.async_response
async def ws_add_template(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict):
    """Create a new template via websocket."""
    data = hass.data[DOMAIN]["data"]
    template_id = str(uuid.uuid4())
    
    new_template = {
        "id": template_id, 
        "name": msg["name"], 
        "description": msg.get("description", ""),
        "area": msg.get("area", ""),
        "complexity": msg["complexity"],
        "icon": msg["icon"],
        "category": normalize_category(msg.get("category")),
        "min_age": normalize_min_age(msg.get("min_age", 0)),
        "requires_approval": bool(msg.get("requires_approval", False)),
        "requires_routine": bool(msg.get("requires_routine", False)),
        "available_from": normalize_time(msg.get("available_from")),
        "available_until": normalize_time(msg.get("available_until")),
        "interval": msg.get("interval"),
        "assignees": msg.get("assignees"),
        "subtasks": msg.get("subtasks", []),
    }
    
    data["templates"][template_id] = new_template
    await hass.data[DOMAIN]["store"].async_save(data)
    hass.bus.async_fire(EVENT_TASK_UPDATED) # Fire update to sync clients
    connection.send_result(msg["id"], {"success": True, "template_id": template_id})

@websocket_api.websocket_command({
    vol.Required("type"): WS_TYPE_EDIT_TEMPLATE,
    vol.Required("template_id"): str, 
    vol.Optional("name"): str,
    vol.Optional("description"): str,
    vol.Optional("area"): str,
    vol.Optional("complexity"): vol.All(int, vol.Range(min=1, max=10)),
    vol.Optional("icon"): str,
    vol.Optional("category"): str,
    vol.Optional("min_age"): vol.All(int, vol.Range(min=0, max=18)),
    vol.Optional("requires_approval"): bool,
    vol.Optional("requires_routine"): bool,
    vol.Optional("available_from"): _TIME_FIELD,
    vol.Optional("available_until"): _TIME_FIELD,
    vol.Optional("interval"): int,
    vol.Optional("assignees"): cv.ensure_list,
    vol.Optional("subtasks"): cv.ensure_list,
})
@websocket_api.async_response
async def ws_edit_template(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict):
    """Edit an existing template via websocket."""
    data = hass.data[DOMAIN]["data"]
    template_id = msg["template_id"]
    
    if template_id not in data.get("templates", {}):
        connection.send_error(msg["id"], "not_found", "Template not found")
        return
        
    template_ref = data["templates"][template_id]
    keys_to_update = ["name", "description", "area", "complexity", "icon", "interval", "assignees", "subtasks"]
    
    for key in keys_to_update:
        if key in msg: 
            template_ref[key] = msg[key]

    if "category" in msg:
        template_ref["category"] = normalize_category(msg["category"])
    if "min_age" in msg:
        template_ref["min_age"] = normalize_min_age(msg["min_age"])
    if "requires_approval" in msg:
        template_ref["requires_approval"] = bool(msg["requires_approval"])
    if "requires_routine" in msg:
        template_ref["requires_routine"] = bool(msg["requires_routine"])
    for key in ("available_from", "available_until"):
        if key in msg:
            template_ref[key] = normalize_time(msg[key])
            
    await hass.data[DOMAIN]["store"].async_save(data)
    hass.bus.async_fire(EVENT_TASK_UPDATED)
    connection.send_result(msg["id"], {"success": True})

@websocket_api.websocket_command({
    vol.Required("type"): WS_TYPE_DELETE_TEMPLATE,
    vol.Required("template_id"): str,
})
@websocket_api.async_response
async def ws_delete_template(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict):
    """Delete a template via websocket."""
    data = hass.data[DOMAIN]["data"]
    template_id = msg["template_id"]
    
    if "templates" not in data:
        data["templates"] = {}

    if template_id in data.get("templates", {}):
        del data["templates"][template_id]
        await hass.data[DOMAIN]["store"].async_save(data)
        hass.bus.async_fire(EVENT_TASK_UPDATED)
    
    connection.send_result(msg["id"], {"success": True})


# ---------------------------------------------------------------------------
# Kids mode
# ---------------------------------------------------------------------------

def _get_entry(hass: HomeAssistant) -> ConfigEntry:
    """Return the (single) config entry of the integration."""
    return hass.config_entries.async_entries(DOMAIN)[0]


def _update_kids(hass: HomeAssistant, kids: dict) -> None:
    """Persist the kid profiles in the settings (config entry options).

    The update listener mirrors the options into ``data["settings"]`` and fires
    ``EVENT_TASK_UPDATED``.
    """
    entry = _get_entry(hass)
    new_options = dict(entry.options)
    new_options["kids"] = kids
    hass.config_entries.async_update_entry(entry, options=new_options)


@websocket_api.websocket_command({
    vol.Required("type"): WS_TYPE_SET_KID_PROFILE,
    vol.Required("participant_id"): str,
    vol.Required("avatar"): vol.All(str, vol.Length(min=1, max=16)),
    vol.Required("color"): str,
    vol.Optional("birth_year", default=None): vol.Any(None, vol.All(int, vol.Range(min=1900, max=2100))),
    vol.Optional("birthday"): vol.Any(None, str),
    vol.Optional("approval_sample_rate"): vol.All(int, vol.Range(min=0, max=100)),
    vol.Optional("pin"): vol.Any(None, str),
    vol.Optional("clear_pin", default=False): bool,
})
@websocket_api.async_response
async def ws_set_kid_profile(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict):
    """Create/update a kid profile (admin only). An empty pin keeps the old one."""
    if not connection.user.is_admin:
        connection.send_error(msg["id"], "unauthorized", "Admin permission required")
        return

    pid = msg["participant_id"]
    known_ids = {
        participant_id(state.entity_id, state.attributes)
        for state in hass.states.async_all("person")
    }
    if pid not in known_ids:
        connection.send_error(msg["id"], "unknown_person", "Unknown person / participant")
        return

    if not is_valid_color(msg["color"]):
        connection.send_error(msg["id"], "invalid_color", "Color must be a hex color like #4CAF50")
        return

    pin = msg.get("pin")
    if pin and not is_valid_pin(pin):
        connection.send_error(msg["id"], "invalid_pin", "PIN must consist of exactly 4 digits")
        return

    extra = {}
    if "birthday" in msg:
        raw = msg["birthday"]
        birthday = parse_birthday(raw) if raw else None
        if raw and (birthday is None or birthday > dt_util.now().date()):
            connection.send_error(msg["id"], "invalid_birthday", "Birthday must be a past date (YYYY-MM-DD)")
            return
        extra["birthday"] = birthday
    if "approval_sample_rate" in msg:
        extra["approval_sample_rate"] = msg["approval_sample_rate"]

    kids = copy.deepcopy(_get_entry(hass).options.get("kids") or {})
    kids[pid] = build_kid_profile(
        kids.get(pid),
        avatar=msg["avatar"].strip() or "🙂",
        color=msg["color"],
        birth_year=msg["birth_year"],
        pin=pin or None,
        clear_pin=msg["clear_pin"],
        **extra,
    )
    if pin or msg["clear_pin"]:
        hass.data[DOMAIN]["pin_limiter"].reset(pid)

    _update_kids(hass, kids)
    connection.send_result(msg["id"], {"success": True})


@websocket_api.websocket_command({
    vol.Required("type"): WS_TYPE_DELETE_KID_PROFILE,
    vol.Required("participant_id"): str,
})
@websocket_api.async_response
async def ws_delete_kid_profile(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict):
    """Remove a kid profile (admin only)."""
    if not connection.user.is_admin:
        connection.send_error(msg["id"], "unauthorized", "Admin permission required")
        return

    pid = msg["participant_id"]
    kids = copy.deepcopy(_get_entry(hass).options.get("kids") or {})
    if pid in kids:
        del kids[pid]
        hass.data[DOMAIN]["pin_limiter"].reset(pid)
        _update_kids(hass, kids)
    connection.send_result(msg["id"], {"success": True})


def _verify_kid(hass: HomeAssistant, kid_id: str, pin) -> tuple[dict, dict, dict]:
    """Verify the PIN of a kid. Returns ``(verification_result, settings, kids)``."""
    settings = _get_entry(hass).options
    kids = settings.get("kids") or {}
    result = verify_kid_pin(kids, hass.data[DOMAIN]["pin_limiter"], kid_id, pin)
    return result, settings, kids


@websocket_api.websocket_command({
    vol.Required("type"): WS_TYPE_KID_VERIFY_PIN,
    vol.Required("kid_id"): str,
    vol.Optional("pin"): vol.Any(None, str),
})
@websocket_api.async_response
async def ws_kid_verify_pin(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict):
    """Check the PIN of a kid. Result: ``{"success": bool, "error"?: code}``."""
    result, _settings, _kids = _verify_kid(hass, msg["kid_id"], msg.get("pin"))
    connection.send_result(msg["id"], result)


def _kid_task_precheck(hass: HomeAssistant, msg: dict) -> tuple[dict | None, dict | None]:
    """Shared PIN + eligibility check of the kid_* task commands.

    :return: ``(error_result, context)``; exactly one of them is not None.
        The context contains ``task``, ``settings`` and ``kid_id``.
    """
    kid_id = msg["kid_id"]
    result, settings, kids = _verify_kid(hass, kid_id, msg.get("pin"))
    if not result["success"]:
        return result, None

    data = hass.data[DOMAIN]["data"]
    task = data["tasks"].get(msg["task_id"])
    if task is None:
        return {"success": False, "error": ERR_NOT_FOUND}, None

    error = check_eligibility(task, kid_id, kids.get(kid_id, {}), data["tasks"], settings, dt_util.now())
    if error:
        result = {"success": False, "error": error}
        if error == ERR_NOT_YET:
            result["available_from"] = normalize_time(task.get("available_from"))
        elif error == ERR_TOO_LATE:
            result["available_until"] = normalize_time(task.get("available_until"))
        return result, None

    return None, {"task": task, "settings": settings, "kid_id": kid_id}


@websocket_api.websocket_command({
    vol.Required("type"): WS_TYPE_KID_COMPLETE_TASK,
    vol.Required("task_id"): str,
    vol.Required("kid_id"): str,
    vol.Optional("pin"): vol.Any(None, str),
})
@websocket_api.async_response
async def ws_kid_complete_task(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict):
    """Complete a task as a kid (PIN + eligibility checked, 100 % of the points)."""
    error, ctx = _kid_task_precheck(hass, msg)
    if error:
        connection.send_result(msg["id"], error)
        return

    # No await between the check above and the completion: atomic in the event loop.
    task = ctx["task"]
    kid_id = ctx["kid_id"]
    points = round(float(task.get("complexity", 1)), 1)
    task_name = task.get("name")

    profile = (ctx["settings"].get("kids") or {}).get(kid_id) or {}
    if needs_approval(task, profile, dt_util.now().date()):
        await _async_request_approval(hass, msg["task_id"], kid_id, points)
        connection.send_result(msg["id"], {"success": True, "pending": True, "points": points, "task_name": task_name})
        return

    success = await _async_complete_task_internal(hass, msg["task_id"], [kid_id], {kid_id: 100})
    if not success:
        connection.send_result(msg["id"], {"success": False, "error": ERR_NOT_FOUND})
        return

    connection.send_result(msg["id"], {"success": True, "points": points, "task_name": task_name})


# ---------------------------------------------------------------------------
# Kids mode step 2: parent approval, penalties, manual adjustments
# ---------------------------------------------------------------------------

def _lang(hass: HomeAssistant) -> str:
    return "de" if str(hass.config.language).lower().startswith("de") else "en"


def _participant_name(hass: HomeAssistant, uid: str) -> str:
    return resolve_participant_name(hass.states.async_all("person"), uid)


def _clean_reason(value) -> str:
    return str(value or "").strip()[:MAX_REASON_LENGTH]


async def _async_request_approval(hass: HomeAssistant, task_id: str, kid_id: str, points: float) -> str:
    """Park a kid's completion until a parent approves it.

    The task advances exactly like a completion (so it cannot be completed
    twice); the points are only awarded on approval.
    """
    data = hass.data[DOMAIN]["data"]
    task = data["tasks"][task_id]
    pending_id = str(uuid.uuid4())
    record = {
        "id": pending_id,
        "task_id": task_id,
        "task_name": task.get("name"),
        "kid_id": kid_id,
        "points": points,
        "timestamp": dt_util.utcnow().isoformat(),
    }
    undo = _advance_task(data, task_id)
    if undo:
        record["undo"] = undo
    data.setdefault("pending", {})[pending_id] = record
    hass.bus.async_fire(EVENT_APPROVAL_REQUESTED, {
        "pending_id": pending_id,
        "task_name": record["task_name"],
        "kid_id": kid_id,
        "kid_name": _participant_name(hass, kid_id),
        "points": points,
    })
    await hass.data[DOMAIN]["store"].async_save(data)
    hass.bus.async_fire(EVENT_TASK_UPDATED)
    return pending_id


def _apply_adjustment(
    hass: HomeAssistant,
    data: dict,
    kid_id: str,
    points: float,
    entry_type: str,
    task_name: str,
    reason: str | None = None,
) -> float:
    """Add a bonus / penalty / birthday entry to the history and the kid's points.

    A penalty never drives the points below 0; the amount that was actually
    applied is recorded. Does not save. Returns the applied amount.
    """
    old_points = data["points"].copy()
    current = float(data["points"].get(kid_id, 0) or 0)
    applied = round(clamp_adjustment(current, points), 1)
    data["points"][kid_id] = round(current + applied, 1)
    entry = {
        "id": str(uuid.uuid4()),
        "task_id": "",
        "task_name": task_name,
        "user_id": kid_id,
        "points": applied,
        "timestamp": dt_util.utcnow().isoformat(),
        "type": entry_type,
    }
    if reason:
        entry["reason"] = reason
    if applied != points:
        entry["requested_points"] = points
    data["history"].insert(0, entry)
    _fire_leaderboard_event_if_changed(hass, old_points, data["points"])
    return applied


def _set_strict_mode(hass: HomeAssistant, kid_id: str, days: int) -> None:
    """Put a kid into strict mode (every task needs approval) for ``days`` days."""
    if days <= 0:
        return
    kids = copy.deepcopy(_get_entry(hass).options.get("kids") or {})
    if kid_id not in kids:
        return
    kids[kid_id]["strict_until"] = (dt_util.now().date() + timedelta(days=days)).isoformat()
    _update_kids(hass, kids)


async def _async_approve_pending(hass: HomeAssistant, pending_id: str) -> dict | None:
    """Award the points of a pending completion. ``None`` for an unknown / already handled id."""
    data = hass.data[DOMAIN]["data"]
    # Removing the record first (no await before) makes this idempotent: two
    # approvals racing from the card and from a push action award only once.
    pending = data.get("pending", {}).pop(pending_id, None)
    if pending is None:
        return None
    points = float(pending.get("points", 0))
    _award_task_points(
        hass, data, pending["task_id"], pending.get("task_name"),
        {pending["kid_id"]: points}, points, None, pending.get("undo"),
    )
    hass.bus.async_fire(EVENT_APPROVAL_RESOLVED, {
        "pending_id": pending_id, "result": "approved", "task_name": pending.get("task_name"),
        "kid_id": pending["kid_id"], "points": points,
    })
    await hass.data[DOMAIN]["store"].async_save(data)
    hass.bus.async_fire(EVENT_TASK_UPDATED)
    return pending


async def _async_reject_pending(
    hass: HomeAssistant, pending_id: str, mode: str, reason: str | None = None
) -> dict | None:
    """Reject a pending completion.

    ``not_done``: no points, the task is due again, no penalty.
    ``cheated``: no points, a penalty and strict mode for the kid.
    """
    data = hass.data[DOMAIN]["data"]
    pending = data.get("pending", {}).pop(pending_id, None)
    if pending is None:
        return None
    kid_id = pending["kid_id"]
    task_name = pending.get("task_name") or ""
    result = {"pending_id": pending_id, "result": mode, "task_name": task_name, "kid_id": kid_id,
              "points": 0, "penalty": 0}

    if mode == "cheated":
        settings = _get_entry(hass).options
        penalty = cheat_penalty(pending.get("points", 0), settings)
        label = ("Geschummelt: " if _lang(hass) == "de" else "Cheated: ") + task_name
        if reason:
            label = f"{label} ({_clean_reason(reason)})"
        label = label[:2 * MAX_REASON_LENGTH]
        if penalty > 0:
            applied = _apply_adjustment(hass, data, kid_id, -penalty, TYPE_PENALTY, label, label)
            result["penalty"] = -applied
        _set_strict_mode(hass, kid_id, cheat_strict_days(settings))
    else:
        task = data["tasks"].get(pending.get("task_id"))
        if task is not None:
            task["due_date"] = dt_util.utcnow().isoformat()
            task["paused_until"] = None

    hass.bus.async_fire(EVENT_APPROVAL_RESOLVED, result)
    await hass.data[DOMAIN]["store"].async_save(data)
    hass.bus.async_fire(EVENT_TASK_UPDATED)
    return pending


@websocket_api.websocket_command({
    vol.Required("type"): WS_TYPE_APPROVE,
    vol.Required("pending_id"): str,
})
@websocket_api.async_response
async def ws_approve(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict):
    """Approve a pending completion and award its points (admin only)."""
    if not connection.user.is_admin:
        connection.send_error(msg["id"], "unauthorized", "Admin permission required")
        return
    pending = await _async_approve_pending(hass, msg["pending_id"])
    if pending is None:
        connection.send_result(msg["id"], {"success": False, "error": ERR_NOT_FOUND})
        return
    connection.send_result(msg["id"], {"success": True, "points": pending.get("points", 0)})


@websocket_api.websocket_command({
    vol.Required("type"): WS_TYPE_REJECT,
    vol.Required("pending_id"): str,
    vol.Required("mode"): vol.In(["not_done", "cheated"]),
    vol.Optional("reason"): vol.Any(None, vol.All(str, vol.Length(max=MAX_REASON_LENGTH))),
})
@websocket_api.async_response
async def ws_reject(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict):
    """Reject a pending completion: ``not_done`` or ``cheated`` (admin only)."""
    if not connection.user.is_admin:
        connection.send_error(msg["id"], "unauthorized", "Admin permission required")
        return
    pending = await _async_reject_pending(hass, msg["pending_id"], msg["mode"], msg.get("reason"))
    if pending is None:
        connection.send_result(msg["id"], {"success": False, "error": ERR_NOT_FOUND})
        return
    connection.send_result(msg["id"], {"success": True})


def _known_participant(hass: HomeAssistant, uid: str) -> bool:
    if uid in (_get_entry(hass).options.get("kids") or {}):
        return True
    return any(
        participant_id(state.entity_id, state.attributes) == uid
        for state in hass.states.async_all("person")
    )


async def _async_adjust_points(hass: HomeAssistant, kid_id: str, points: int, reason: str) -> float:
    data = hass.data[DOMAIN]["data"]
    entry_type = TYPE_BONUS if points > 0 else TYPE_PENALTY
    applied = _apply_adjustment(hass, data, kid_id, points, entry_type, reason, reason)
    await hass.data[DOMAIN]["store"].async_save(data)
    hass.bus.async_fire(EVENT_TASK_UPDATED)
    return applied


@websocket_api.websocket_command({
    vol.Required("type"): WS_TYPE_ADJUST_POINTS,
    vol.Required("kid_id"): str,
    vol.Required("points"): vol.All(int, vol.Range(min=-MAX_ADJUST_POINTS, max=MAX_ADJUST_POINTS)),
    vol.Required("reason"): vol.All(str, vol.Length(max=MAX_REASON_LENGTH)),
})
@websocket_api.async_response
async def ws_adjust_points(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict):
    """Give a bonus or a penalty with a reason (admin only)."""
    if not connection.user.is_admin:
        connection.send_error(msg["id"], "unauthorized", "Admin permission required")
        return
    reason = _clean_reason(msg["reason"])
    if not reason:
        connection.send_error(msg["id"], "invalid_reason", "A reason is required")
        return
    if msg["points"] == 0:
        connection.send_error(msg["id"], "invalid_points", "Points must not be 0")
        return
    kid_id = _get_user_id(hass, msg["kid_id"])
    if not _known_participant(hass, kid_id):
        connection.send_error(msg["id"], "unknown_person", "Unknown person / participant")
        return
    applied = await _async_adjust_points(hass, kid_id, msg["points"], reason)
    connection.send_result(msg["id"], {
        "success": True, "applied": applied,
        "total": hass.data[DOMAIN]["data"]["points"].get(kid_id, 0),
    })


# ---------------------------------------------------------------------------
# Undo a completion (admin only)
# ---------------------------------------------------------------------------

def _restore_task_after_undo(data: dict, task_id: str, source: dict | None) -> None:
    """Make the task open again: previous due date, subtasks undone.

    ``source`` is a history entry of the completion carrying the undo
    information (``None`` / missing keys for entries written by older
    versions: the task is then simply due now). A one-time task that was
    deleted by the completion is recreated from its snapshot.
    """
    source = source or {}
    due = source.get("previous_due_date") or dt_util.utcnow().isoformat()
    paused = source.get("previous_paused_until")
    task = data["tasks"].get(task_id)
    if task is None:
        snapshot = source.get("task_snapshot")
        if not isinstance(snapshot, dict):
            return
        task = copy.deepcopy(snapshot)
        task["id"] = task_id
        task.pop("claimed_by", None)
        task.pop("claimed_until", None)
        data["tasks"][task_id] = task
    task["due_date"] = due
    task["paused_until"] = paused
    task.pop("claimed_by", None)
    task.pop("claimed_until", None)
    subtasks = task.get("subtasks")
    if not subtasks and source.get("subtasks"):
        subtasks = copy.deepcopy(source["subtasks"])
    if subtasks:
        task["subtasks"] = [{**st, "done": False} for st in subtasks if isinstance(st, dict)]


@websocket_api.websocket_command({
    vol.Required("type"): WS_TYPE_UNDO_COMPLETION,
    vol.Required("history_id"): str,
})
@websocket_api.async_response
async def ws_undo_completion(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict):
    """Undo a task completion (admin only).

    Removes all history entries of that completion, takes their points back
    (never below 0) and makes the task open again without any penalty. A
    second undo of the same entry answers ``not_found``.
    """
    if not connection.user.is_admin:
        connection.send_error(msg["id"], "unauthorized", "Admin permission required")
        return
    data = hass.data[DOMAIN]["data"]
    history = data["history"]
    entry = next(
        (h for h in history if h.get("id") == msg["history_id"] and h.get("type", TYPE_TASK) == TYPE_TASK),
        None,
    )
    if entry is None:
        connection.send_result(msg["id"], {"success": False, "error": ERR_NOT_FOUND})
        return

    completion_id = entry.get("completion_id")
    group = [h for h in history if h.get("completion_id") == completion_id] if completion_id else [entry]
    group_ids = {h.get("id") for h in group}

    old_points = data["points"].copy()
    removed_points = {}
    for h in group:
        uid = h.get("user_id")
        try:
            pts = float(h.get("points", 0))
        except (TypeError, ValueError):
            pts = 0.0
        if uid in data["points"]:
            data["points"][uid] = round(max(0.0, float(data["points"][uid]) - pts), 1)
        removed_points[uid] = round(removed_points.get(uid, 0.0) + pts, 1)
    data["history"] = [h for h in history if h.get("id") not in group_ids]

    source = next((h for h in group if "previous_due_date" in h or "task_snapshot" in h), None)
    if entry.get("task_id"):
        _restore_task_after_undo(data, entry["task_id"], source)

    _fire_leaderboard_event_if_changed(hass, old_points, data["points"])
    await hass.data[DOMAIN]["store"].async_save(data)
    hass.bus.async_fire(EVENT_TASK_UPDATED)
    connection.send_result(msg["id"], {
        "success": True, "removed": len(group), "points": removed_points,
        "task_id": entry.get("task_id"),
    })


# ---------------------------------------------------------------------------
# Ad-hoc bonus (admin only)
# ---------------------------------------------------------------------------

MAX_ADHOC_NAME_LENGTH = 60
_ICON_RE = re.compile(r"^mdi:[a-z0-9-]{1,60}$")


@websocket_api.websocket_command({
    vol.Required("type"): WS_TYPE_ADD_ADHOC_BONUS,
    vol.Required("name"): str,
    vol.Optional("icon", default="mdi:star"): str,
    vol.Required("points"): vol.All(int, vol.Range(min=1, max=10)),
    vol.Optional("minutes"): vol.Any(None, vol.All(int, vol.Range(min=1, max=600))),
    vol.Optional("kid_id"): vol.Any(None, str),
    vol.Optional("min_age", default=0): vol.All(int, vol.Range(min=0, max=18)),
    vol.Optional("expires_at"): vol.Any(None, str),
})
@websocket_api.async_response
async def ws_add_adhoc_bonus(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict):
    """Create a spontaneous one-time bonus task (admin only).

    Open to all kids or targeted at one kid; optionally expires on its own.
    """
    if not connection.user.is_admin:
        connection.send_error(msg["id"], "unauthorized", "Admin permission required")
        return
    name = msg["name"].strip()
    if not name or len(name) > MAX_ADHOC_NAME_LENGTH:
        connection.send_error(msg["id"], "invalid_name", f"Name is required (max {MAX_ADHOC_NAME_LENGTH} characters)")
        return
    if not _ICON_RE.match(msg["icon"]):
        connection.send_error(msg["id"], "invalid_icon", "Icon must look like mdi:cart")
        return
    assignees = []
    if msg.get("kid_id"):
        kid_id = _get_user_id(hass, msg["kid_id"])
        if not _known_participant(hass, kid_id):
            connection.send_error(msg["id"], "unknown_person", "Unknown person / participant")
            return
        assignees = [kid_id]
    expires_at = None
    if msg.get("expires_at"):
        parsed = dt_util.parse_datetime(msg["expires_at"])
        if parsed is None:
            connection.send_error(msg["id"], "invalid_expiry", "expires_at must be an ISO date/time")
            return
        parsed = dt_util.as_utc(parsed)
        if parsed <= dt_util.utcnow():
            connection.send_error(msg["id"], "invalid_expiry", "expires_at must be in the future")
            return
        expires_at = parsed.isoformat()

    data = hass.data[DOMAIN]["data"]
    task_id = str(uuid.uuid4())
    task = {
        "id": task_id,
        "name": name,
        "description": "",
        "area": "",
        "interval": 0,
        "assignees": assignees,
        "complexity": msg["points"],
        "category": "bonus",
        "min_age": normalize_min_age(msg.get("min_age", 0)),
        "requires_approval": False,
        "requires_routine": False,
        "available_from": None,
        "available_until": None,
        "icon": msg["icon"],
        "due_date": dt_util.utcnow().isoformat(),
        "paused_until": None,
        "override_overdue_days": None,
        "subtasks": [],
        "adhoc": True,
        "expires_at": expires_at,
    }
    if msg.get("minutes"):
        task["minutes"] = msg["minutes"]
    data["tasks"][task_id] = task
    hass.bus.async_fire(EVENT_TASK_CREATED, dict(task))
    hass.bus.async_fire(EVENT_BONUS_OPENED, {
        "task_id": task_id,
        "task_name": name,
        "points": msg["points"],
        "adhoc": True,
    })
    await hass.data[DOMAIN]["store"].async_save(data)
    hass.bus.async_fire(EVENT_TASK_UPDATED)
    connection.send_result(msg["id"], {"success": True, "task_id": task_id})


async def _async_remove_expired_adhoc(hass: HomeAssistant) -> int:
    """Delete ad-hoc tasks whose ``expires_at`` has passed (no points, no history)."""
    if DOMAIN not in hass.data:
        return 0
    data = hass.data[DOMAIN]["data"]
    now = dt_util.now()
    expired = [
        tid for tid, task in data["tasks"].items()
        if isinstance(task, dict) and task.get("adhoc") and is_expired(task, now)
    ]
    if not expired:
        return 0
    for tid in expired:
        del data["tasks"][tid]
    await hass.data[DOMAIN]["store"].async_save(data)
    hass.bus.async_fire(EVENT_TASK_UPDATED)
    return len(expired)


# ---- push notification actions -------------------------------------------------

async def _async_handle_push_action(hass: HomeAssistant, action: str) -> None:
    """Execute ``TO_APPROVE_<id>`` / ``TO_NOTDONE_<id>`` / ``TO_CHEAT_<id>``."""
    if DOMAIN not in hass.data:
        return
    for prefix, mode in ((ACTION_APPROVE, "approve"), (ACTION_NOT_DONE, "not_done"), (ACTION_CHEAT, "cheated")):
        if action.startswith(prefix):
            pending_id = action[len(prefix):]
            break
    else:
        return
    if mode == "approve":
        result = await _async_approve_pending(hass, pending_id)
    else:
        result = await _async_reject_pending(hass, pending_id, mode)
    if result is None:
        _LOGGER.debug("Notification action %s ignored: unknown or already handled id", action)


# ---------------------------------------------------------------------------
# Kids mode step 2: birthday
# ---------------------------------------------------------------------------

def _birthday_unlocks(hass: HomeAssistant, data: dict, kid_id: str, settings: dict, old_age: int | None, new_age: int) -> list[dict]:
    """What the new age unlocks: tasks, templates and catalog entries (deduplicated)."""
    lang = _lang(hass)
    found = []
    candidates = [
        t for t in data["tasks"].values()
        if t.get("generated_by") != kids_catalog.GENERATED_BY
        and (not t.get("assignees") or kid_id in (t.get("assignees") or []))
    ]
    candidates += [
        t for t in data.get("templates", {}).values()
        if t.get("generated_by") != kids_catalog.GENERATED_BY
    ]
    for item in unlocked_by_age(candidates, old_age, new_age):
        found.append({"name": item.get("name") or "", "icon": item.get("icon") or "mdi:star"})
    features = (settings.get("wizard") or {}).get("features")
    for entry in kids_catalog.newly_unlocked(old_age, new_age, features, lang):
        found.append({"name": entry["name"], "icon": entry["icon"]})
    seen = set()
    result = []
    for item in found:
        key = item["name"].strip().lower()
        if key and key not in seen:
            seen.add(key)
            result.append(item)
    return result


def _skip_duties_for(hass: HomeAssistant, data: dict, kid_id: str, settings: dict, now: datetime) -> int:
    """Skip the basis tasks that belong ONLY to this kid and are due today (no points, no history)."""
    overdue_days = settings.get("overdue_days", 5)
    skipped = 0
    for task_id, task in list(data["tasks"].items()):
        assignees = task.get("assignees") or []
        if isinstance(assignees, str):
            assignees = [assignees]
        if (
            list(assignees) == [kid_id]
            and normalize_category(task.get("category")) == "basis"
            and task.get("interval", 7) > 0
            and is_open(task, now, overdue_days)
        ):
            _advance_task(data, task_id)
            skipped += 1
    return skipped


async def _async_handle_birthdays(hass: HomeAssistant) -> None:
    """Once per birthday: duty-free day, bonus points, level-up info, event + notification."""
    if DOMAIN not in hass.data:
        return
    data = hass.data[DOMAIN]["data"]
    settings = _get_entry(hass).options
    kids = settings.get("kids") or {}
    now = dt_util.now()
    today = now.date()
    today_key = today.isoformat()
    handled = data.setdefault("birthday_handled", {})
    levelup = data.setdefault("birthday_levelup", {})
    changed = False

    for kid_id in [k for k, v in levelup.items() if v.get("date") != today_key]:
        del levelup[kid_id]
        changed = True

    lang = _lang(hass)
    for kid_id in birthday_kids(kids, today):
        if handled.get(kid_id) == today_key:
            continue
        # Mark first: nothing below may be applied twice
        handled[kid_id] = today_key
        changed = True
        profile = kids[kid_id]
        new_age = kid_age(profile, today)
        old_age = new_age - 1 if new_age is not None else None
        name = _participant_name(hass, kid_id)

        skipped = _skip_duties_for(hass, data, kid_id, settings, now) if birthday_duty_free(settings) else 0
        bonus = birthday_bonus_points(settings)
        if bonus > 0:
            _apply_adjustment(
                hass, data, kid_id, bonus, TYPE_BIRTHDAY,
                "🎂 Geburtstags-Bonus" if lang == "de" else "🎂 Birthday bonus",
            )
        unlocked = _birthday_unlocks(hass, data, kid_id, settings, old_age, new_age)
        levelup[kid_id] = {"date": today_key, "age": new_age, "unlocked": unlocked, "skipped": skipped}

        hass.bus.async_fire(EVENT_KID_BIRTHDAY, {
            "kid_id": kid_id, "name": name, "age": new_age, "unlocked": [u["name"] for u in unlocked],
        })
        if lang == "de":
            message = (f"{name} ist heute {new_age} geworden – im Kindermodus-Schnellstart "
                       "ein neues Aufgaben-Paket vorschlagen lassen.")
            title = "🎂 Geburtstag"
        else:
            message = (f"{name} turned {new_age} today - let the kids mode quick-start "
                       "suggest a new task package.")
            title = "🎂 Birthday"
        persistent_notification.async_create(
            hass, message, title=title, notification_id=f"{DOMAIN}_birthday_{kid_id}_{today_key}",
        )

    if changed:
        await hass.data[DOMAIN]["store"].async_save(data)
        hass.bus.async_fire(EVENT_TASK_UPDATED)


@websocket_api.websocket_command({
    vol.Required("type"): WS_TYPE_KID_CLAIM_TASK,
    vol.Required("task_id"): str,
    vol.Required("kid_id"): str,
    vol.Optional("pin"): vol.Any(None, str),
})
@websocket_api.async_response
async def ws_kid_claim_task(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict):
    """Reserve a (bonus) task for a kid for ``claim_minutes`` (default 10)."""
    error, ctx = _kid_task_precheck(hass, msg)
    if error:
        connection.send_result(msg["id"], error)
        return

    claim_minutes = ctx["settings"].get("claim_minutes", DEFAULT_CLAIM_MINUTES)
    if isinstance(claim_minutes, bool) or not isinstance(claim_minutes, (int, float)) or claim_minutes <= 0:
        claim_minutes = DEFAULT_CLAIM_MINUTES

    task = ctx["task"]
    claimed_until = (dt_util.utcnow() + timedelta(minutes=claim_minutes)).isoformat()
    task["claimed_by"] = ctx["kid_id"]
    task["claimed_until"] = claimed_until

    data = hass.data[DOMAIN]["data"]
    await hass.data[DOMAIN]["store"].async_save(data)
    hass.bus.async_fire(EVENT_TASK_UPDATED)
    connection.send_result(msg["id"], {"success": True, "claimed_until": claimed_until})


# ---------------------------------------------------------------------------
# Kids mode: quick-start wizard
# ---------------------------------------------------------------------------

_WIZARD_KID_SCHEMA = vol.Schema({
    vol.Required("id"): str,
    vol.Optional("name"): str,
    vol.Optional("birth_year"): vol.Any(None, vol.All(int, vol.Range(min=1900, max=2100))),
    vol.Optional("birthday"): vol.Any(None, str),
    vol.Optional("avatar"): vol.All(str, vol.Length(min=1, max=16)),
    vol.Optional("color"): str,
    vol.Optional("pin"): vol.Any(None, str),
})

_WIZARD_BASE_SCHEMA = {
    vol.Required("kids"): [_WIZARD_KID_SCHEMA],
    vol.Optional("features", default=[]): [str],
    vol.Optional("start_date"): str,
    vol.Optional("phase", default="start"): vol.In(["start", "normal"]),
    vol.Optional("language"): str,
    vol.Optional("excluded", default=[]): [str],
    vol.Optional("points", default={}): {str: vol.Any(int, float)},
    vol.Optional("household_day"): vol.All(int, vol.Range(min=0, max=6)),
}


class _WizardError(Exception):
    """Validation error of a wizard command (code + message)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _wizard_context(hass: HomeAssistant, msg: dict) -> dict:
    """Validate the common wizard input and return the normalized context."""
    person_names = {}
    for state in hass.states.async_all("person"):
        person_names[participant_id(state.entity_id, state.attributes)] = (
            state.attributes.get("friendly_name") or state.entity_id
        )

    now = dt_util.now()
    start = kids_catalog.parse_date(msg.get("start_date"), now.date())
    kids = []
    seen = set()
    for kid in msg["kids"]:
        kid_id = kid["id"]
        if kid_id not in person_names:
            raise _WizardError("unknown_person", f"Unknown person / participant: {kid_id}")
        if kid_id in seen:
            continue
        seen.add(kid_id)
        if "color" in kid and not is_valid_color(kid["color"]):
            raise _WizardError("invalid_color", "Color must be a hex color like #4CAF50")
        if kid.get("pin") and not is_valid_pin(kid["pin"]):
            raise _WizardError("invalid_pin", "PIN must consist of exactly 4 digits")
        birthday = None
        if kid.get("birthday"):
            birthday = parse_birthday(kid["birthday"])
            if birthday is None or birthday > now.date():
                raise _WizardError("invalid_birthday", "Birthday must be a past date (YYYY-MM-DD)")
        birth_year = birthday.year if birthday else kid.get("birth_year")
        kids.append({
            "id": kid_id,
            "name": person_names[kid_id],
            "birth_year": birth_year,
            "birthday": birthday.isoformat() if birthday else None,
            # exact age at the start of the plan (birthday known) or by birth year
            "age": kid_age({"birth_year": birth_year, "birthday": birthday}, start),
            "avatar": kid.get("avatar"),
            "color": kid.get("color"),
            "pin": kid.get("pin"),
        })

    language = msg.get("language") or hass.config.language or "en"
    return {
        "kids": kids,
        "features": [f for f in msg.get("features", []) if f in kids_catalog.FEATURE_KEYS],
        "start": start,
        "phase": msg.get("phase", kids_catalog.PHASE_START),
        "lang": "de" if str(language).lower().startswith("de") else "en",
        "excluded": set(msg.get("excluded", [])),
        "points": {k: v for k, v in (msg.get("points") or {}).items()},
        "household_day": msg.get("household_day", kids_catalog.DEFAULT_HOUSEHOLD_DAY),
    }


def _wizard_proposal(ctx: dict, excluded: set | None = None) -> dict:
    return kids_catalog.build_proposal(
        ctx["kids"], ctx["features"], ctx["start"], ctx["phase"],
        lang=ctx["lang"], excluded=ctx["excluded"] if excluded is None else excluded,
        points=ctx["points"], household_day=ctx["household_day"],
    )


@websocket_api.websocket_command({vol.Required("type"): WS_TYPE_WIZARD_CATALOG})
@websocket_api.async_response
async def ws_wizard_catalog(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict):
    """Return the task catalog and the household features (admin only)."""
    if not connection.user.is_admin:
        connection.send_error(msg["id"], "unauthorized", "Admin permission required")
        return
    result = kids_catalog.catalog_for_client()
    result["today"] = dt_util.now().date().isoformat()
    result["language"] = "de" if str(hass.config.language).lower().startswith("de") else "en"
    result["wizard"] = _get_entry(hass).options.get("wizard") or {}
    result["has_wizard_tasks"] = any(
        t.get("generated_by") == kids_catalog.GENERATED_BY
        for t in hass.data[DOMAIN]["data"]["tasks"].values()
    )
    connection.send_result(msg["id"], result)


@websocket_api.websocket_command({
    vol.Required("type"): WS_TYPE_WIZARD_PREVIEW,
    **_WIZARD_BASE_SCHEMA,
})
@websocket_api.async_response
async def ws_wizard_preview(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict):
    """Compute the proposal (tasks per kid, rotation, load, goals) without saving."""
    if not connection.user.is_admin:
        connection.send_error(msg["id"], "unauthorized", "Admin permission required")
        return
    try:
        ctx = _wizard_context(hass, msg)
    except _WizardError as err:
        connection.send_error(msg["id"], err.code, err.message)
        return
    proposal = _wizard_proposal(ctx)
    proposal["suggested_excluded"] = kids_catalog.suggest_excluded(_wizard_proposal(ctx, set()), ctx["phase"])
    for kid in proposal["kids"]:
        kid.pop("pin", None)
    connection.send_result(msg["id"], proposal)


def _wizard_update_task(task: dict, item: dict) -> None:
    """Refresh the definition of an existing wizard task from the proposal.

    Due date, pause, claim and the assignees stay as they are; the progress of
    subtasks with an unchanged title is kept.
    """
    progress = {st.get("title"): bool(st.get("done")) for st in task.get("subtasks") or [] if isinstance(st, dict)}
    task["name"] = item["name"]
    task["complexity"] = item["points"]
    task["icon"] = item["icon"]
    task["category"] = normalize_category(item["category"])
    task["min_age"] = normalize_min_age(item["min_age"])
    task["interval"] = item["interval"]
    task["available_from"] = normalize_time(item.get("available_from"))
    task["available_until"] = normalize_time(item.get("available_until"))
    task["subtasks"] = [
        {**st, "done": progress.get(st.get("title"), False)} for st in copy.deepcopy(item["subtasks"])
    ]


def _wizard_restagger(wizard_tasks: list, existing: dict, household_day: int) -> dict:
    """New due dates of rotations whose members or length changed.

    The turns are staggered again starting at the NEXT occurrence (tomorrow, or
    the next household day for weekly rotations) - never today.
    """
    tomorrow = dt_util.now().date() + timedelta(days=1)
    result = {}
    by_catalog: dict[str, list] = {}
    for item in wizard_tasks:
        if item["mode"] == kids_catalog.MODE_ROTATION and item["selected"]:
            by_catalog.setdefault(item["catalog_key"], []).append(item)
    for catalog_key, members in by_catalog.items():
        old = {
            kids_catalog.split_key(key)[1]: task for key, task in existing.items()
            if kids_catalog.split_key(key)[0] == catalog_key
        }
        if not old:
            continue  # brand-new rotation: the proposal's dates apply
        same_members = set(old) == {m["kid_id"] for m in members}
        same_length = all(old[m["kid_id"]].get("interval") == m["interval"] for m in members if m["kid_id"] in old)
        if same_members and same_length:
            continue
        base = members[0]["base_interval"]
        first = kids_catalog.next_weekday(tomorrow, household_day) if base >= 7 else tomorrow
        for m in members:
            result[m["wizard_key"]] = first + timedelta(days=base * (m["rotation_index"] or 0))
    return result


def _wizard_task_ids_to_replace(tasks: dict, kid_ids: set, proposal: dict) -> list[str]:
    """IDs of earlier wizard tasks that the new run regenerates.

    Manual tasks (no ``generated_by == "wizard"``) are never touched.
    """
    regenerated_rotations = {
        t["catalog_key"] for k in proposal["kids"] for t in k["tasks"] if t["mode"] == kids_catalog.MODE_ROTATION
    }
    result = []
    for tid, task in tasks.items():
        if task.get("generated_by") != kids_catalog.GENERATED_BY or not task.get("wizard_key"):
            continue
        catalog_key, suffix = kids_catalog.split_key(task["wizard_key"])
        if (
            suffix in kid_ids
            or suffix == kids_catalog.OPEN_SUFFIX
            or catalog_key in regenerated_rotations
        ):
            result.append(tid)
    return result


@websocket_api.websocket_command({
    vol.Required("type"): WS_TYPE_WIZARD_APPLY,
    **_WIZARD_BASE_SCHEMA,
    vol.Optional("selection"): [str],
    vol.Optional("goals", default={}): {str: vol.All(int, vol.Range(min=0, max=99999))},
    vol.Optional("family_goal"): vol.Schema({
        vol.Required("points"): vol.All(int, vol.Range(min=0, max=999999)),
        vol.Optional("reward", default=""): vol.All(str, vol.Length(max=80)),
    }),
    vol.Optional("replace_existing", default=False): bool,
    vol.Optional("device_triggers", default=[]): [vol.Schema({
        vol.Required("wizard_key"): str,
        vol.Required("entity_id"): cv.entity_id,
        vol.Required("to_state"): vol.All(str, vol.Length(min=1, max=255)),
    })],
})
@websocket_api.async_response
async def ws_wizard_apply(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict):
    """Create the whole kids setup in one go (admin only).

    Everything is validated first; after that tasks, templates, kid profiles,
    goals and triggers are written without any await in between and saved once.
    """
    if not connection.user.is_admin:
        connection.send_error(msg["id"], "unauthorized", "Admin permission required")
        return
    try:
        ctx = _wizard_context(hass, msg)
        candidates = kids_catalog.all_candidate_keys(_wizard_proposal(ctx, set()))
        if "selection" in msg:
            excluded = candidates - set(msg["selection"])
        else:
            excluded = ctx["excluded"]
        proposal = _wizard_proposal(ctx, excluded)
        for trig in msg["device_triggers"]:
            catalog_key, suffix = kids_catalog.split_key(trig["wizard_key"])
            if suffix != kids_catalog.OPEN_SUFFIX or catalog_key not in kids_catalog.OPEN_BONUS_KEYS:
                raise _WizardError("invalid_trigger", "Triggers are only possible for open bonus tasks")
    except _WizardError as err:
        connection.send_error(msg["id"], err.code, err.message)
        return

    data = hass.data[DOMAIN]["data"]
    entry = _get_entry(hass)
    kid_ids = {k["id"] for k in ctx["kids"]}

    # --- kid profiles (existing PINs are kept) ---------------------------
    suggestions = kids_catalog.suggest_profiles([k["id"] for k in ctx["kids"]])
    kids_opt = copy.deepcopy(entry.options.get("kids") or {})
    for kid in ctx["kids"]:
        old = kids_opt.get(kid["id"]) or {}
        kids_opt[kid["id"]] = build_kid_profile(
            old,
            avatar=(kid["avatar"] or old.get("avatar") or suggestions[kid["id"]]["avatar"]).strip() or "🙂",
            color=kid["color"] or old.get("color") or suggestions[kid["id"]]["color"],
            birth_year=kid["birth_year"],
            pin=kid["pin"] or None,
            **({"birthday": kid["birthday"]} if kid["birthday"] else {}),
        )
        if kid["pin"]:
            hass.data[DOMAIN]["pin_limiter"].reset(kid["id"])

    # --- tasks ---------------------------------------------------------------
    # A re-run (replace_existing) updates earlier wizard tasks IN PLACE: due date,
    # pause, claim and subtask progress are kept, so the day is not "reset".
    replace = msg["replace_existing"]
    wizard_tasks = [t for k in proposal["kids"] for t in k["tasks"]] + proposal["open_tasks"]
    selected_keys = {item["wizard_key"] for item in wizard_tasks if item["selected"]}

    def wizard_task_by_key() -> dict:
        return {
            t["wizard_key"]: t for t in data["tasks"].values()
            if t.get("generated_by") == kids_catalog.GENERATED_BY and t.get("wizard_key")
        }

    restagger = _wizard_restagger(wizard_tasks, wizard_task_by_key(), ctx["household_day"]) if replace else {}

    deleted = 0
    if replace:
        for tid in _wizard_task_ids_to_replace(data["tasks"], kid_ids, proposal):
            if data["tasks"][tid].get("wizard_key") not in selected_keys:
                del data["tasks"][tid]
                deleted += 1
    existing = wizard_task_by_key()

    def to_due(day: str) -> str:
        return dt_util.as_utc(dt_util.start_of_local_day(date.fromisoformat(day))).isoformat()

    created_tasks = []
    skipped = 0
    updated = 0
    for item in wizard_tasks:
        if not item["selected"]:
            continue
        old = existing.get(item["wizard_key"])
        if old is not None and not replace:
            skipped += 1
            continue
        if old is not None:
            _wizard_update_task(old, item)
            if item["wizard_key"] in restagger:
                old["due_date"] = to_due(restagger[item["wizard_key"]].isoformat())
                old["paused_until"] = None
                old.pop("claimed_by", None)
                old.pop("claimed_until", None)
            updated += 1
            continue
        task_id = str(uuid.uuid4())
        due_day = restagger.get(item["wizard_key"]) or item["due_date"]
        due_day = due_day.isoformat() if isinstance(due_day, date) else due_day
        task = {
            "id": task_id,
            "name": item["name"],
            "description": "",
            "area": "",
            "interval": item["interval"],
            "assignees": [item["kid_id"]] if item["kid_id"] else [],
            "complexity": item["points"],
            "category": normalize_category(item["category"]),
            "min_age": normalize_min_age(item["min_age"]),
            "available_from": normalize_time(item.get("available_from")),
            "available_until": normalize_time(item.get("available_until")),
            "icon": item["icon"],
            "due_date": to_due(due_day),
            "paused_until": None,
            "override_overdue_days": None,
            "subtasks": copy.deepcopy(item["subtasks"]),
            "wizard_key": item["wizard_key"],
            "generated_by": kids_catalog.GENERATED_BY,
        }
        data["tasks"][task_id] = task
        created_tasks.append(task)

    # --- Auftrag templates (never duplicated by name) ----------------------------
    templates = data.setdefault("templates", {})
    template_names = {str(t.get("name", "")).strip().lower() for t in templates.values()}
    created_templates = 0
    for item in proposal["templates"]:
        if not item["selected"] or item["name"].strip().lower() in template_names:
            continue
        template_id = str(uuid.uuid4())
        templates[template_id] = {
            "id": template_id,
            "name": item["name"],
            "description": "",
            "area": "",
            "complexity": item["points"],
            "icon": item["icon"],
            "category": "auftrag",
            "min_age": normalize_min_age(item["min_age"]),
            "interval": 0,
            "assignees": [],
            "subtasks": [],
        }
        template_names.add(item["name"].strip().lower())
        created_templates += 1

    # --- settings: goals, family goal, triggers, wizard memory ---------------------
    options = dict(entry.options)
    options["kids"] = kids_opt
    point_goals = dict(options.get("point_goals") or {})
    full_goals = dict(options.get("point_goals_full") or {})
    prorated = bool(proposal["prorated"])
    for kid in proposal["kids"]:
        goal = msg["goals"].get(kid["id"], kid["goal"])
        if goal > 0:
            point_goals[kid["id"]] = goal
        else:
            point_goals.pop(kid["id"], None)
        # The start month is prorated: remember the full goal for the next months
        if goal > 0 and prorated:
            if goal == kid["goal"]:
                full_goals[kid["id"]] = kid["goal_full"]
            else:  # edited in the summary: scale the edited value back up
                full_goals[kid["id"]] = max(goal, kids_catalog.round_to(goal / proposal["prorate"], 5))
        else:
            full_goals.pop(kid["id"], None)
    options["point_goals"] = point_goals
    options["point_goals_full"] = full_goals
    if prorated and any(full_goals.get(k["id"]) for k in proposal["kids"]):
        options["point_goals_prorated_month"] = proposal["prorated_month"]
    elif not full_goals:
        options.pop("point_goals_prorated_month", None)
    if "family_goal" in msg:
        options["family_goal"] = {"points": msg["family_goal"]["points"], "reward": msg["family_goal"].get("reward", "").strip()}

    open_keys = {kids_catalog.make_key(k, kids_catalog.OPEN_SUFFIX) for k in kids_catalog.OPEN_BONUS_KEYS}
    triggers = [
        t for t in (options.get("bonus_triggers") or []) if t.get("wizard_key") not in open_keys
    ]
    triggers += [
        {"wizard_key": t["wizard_key"], "entity_id": t["entity_id"], "to_state": t["to_state"]}
        for t in msg["device_triggers"]
    ]
    options["bonus_triggers"] = triggers
    options["wizard"] = {
        "features": ctx["features"],
        "phase": ctx["phase"],
        "excluded": sorted(excluded & candidates),
        "points": {k: v for k, v in ctx["points"].items() if k in candidates},
        "last_run": dt_util.utcnow().isoformat(),
        "household_day": ctx["household_day"],
    }
    hass.config_entries.async_update_entry(entry, options=options)

    for task in created_tasks:
        hass.bus.async_fire(EVENT_TASK_CREATED, dict(task))
    await hass.data[DOMAIN]["store"].async_save(data)
    hass.bus.async_fire(EVENT_TASK_UPDATED)

    created_open = sum(1 for t in created_tasks if not t["assignees"])
    connection.send_result(msg["id"], {
        "success": True,
        "created": len(created_tasks) - created_open,
        "bonus": created_open,
        "templates": created_templates,
        "deleted": deleted,
        "updated": updated,
        "skipped": skipped,
        "triggers": len(msg["device_triggers"]),
    })


# ---------------------------------------------------------------------------
# Kids mode: device triggers for bonus tasks
# ---------------------------------------------------------------------------

def _valid_bonus_triggers(settings: dict) -> list[dict]:
    """Well-formed trigger entries of the settings (anything else is ignored)."""
    result = []
    for trig in settings.get("bonus_triggers") or []:
        if (
            isinstance(trig, dict)
            and isinstance(trig.get("entity_id"), str) and trig["entity_id"]
            and isinstance(trig.get("to_state"), str)
            and (trig.get("task_id") or trig.get("wizard_key"))
        ):
            result.append(trig)
    return result


def _find_trigger_task(tasks: dict, trig: dict) -> dict | None:
    if trig.get("task_id") and trig["task_id"] in tasks:
        return tasks[trig["task_id"]]
    if trig.get("wizard_key"):
        for task in tasks.values():
            if task.get("wizard_key") == trig["wizard_key"]:
                return task
    return None


async def _async_open_bonus_task(hass: HomeAssistant, trig: dict) -> None:
    """Make the bonus task of a trigger due now and announce it on the event bus."""
    if DOMAIN not in hass.data:
        return
    data = hass.data[DOMAIN]["data"]
    task = _find_trigger_task(data["tasks"], trig)
    if task is None:
        _LOGGER.debug("Bonus trigger %s: task not found", trig)
        return
    if is_open(task, dt_util.now(), data.get("settings", {}).get("overdue_days", 5)):
        return  # already open: no new announcement
    task["due_date"] = dt_util.utcnow().isoformat()
    task["paused_until"] = None
    await hass.data[DOMAIN]["store"].async_save(data)
    hass.bus.async_fire(EVENT_BONUS_OPENED, {
        "task_id": task["id"],
        "task_name": task.get("name"),
        "points": task.get("complexity"),
    })
    hass.bus.async_fire(EVENT_TASK_UPDATED)


def async_setup_bonus_triggers(hass: HomeAssistant) -> None:
    """(Re-)register the state listeners of the bonus triggers in the settings."""
    domain_data = hass.data.get(DOMAIN)
    if domain_data is None:
        return
    unsub = domain_data.pop("bonus_unsub", None)
    if unsub is not None:
        unsub()

    by_entity: dict[str, list[dict]] = {}
    for trig in _valid_bonus_triggers(domain_data["data"].get("settings", {})):
        by_entity.setdefault(trig["entity_id"], []).append(trig)
    if not by_entity:
        return

    @callback
    def _state_changed(event) -> None:
        new_state = event.data.get("new_state")
        old_state = event.data.get("old_state")
        # Only real transitions: not the first state after a (re)start and not
        # the recovery from unknown/unavailable (restart, lost connection).
        if new_state is None or old_state is None:
            return
        if old_state.state == new_state.state or old_state.state in ("unknown", "unavailable"):
            return
        for trig in by_entity.get(new_state.entity_id, []):
            if new_state.state == trig["to_state"]:
                hass.async_create_task(_async_open_bonus_task(hass, trig))

    domain_data["bonus_unsub"] = async_track_state_change_event(hass, list(by_entity), _state_changed)


@websocket_api.websocket_command({
    vol.Required("type"): WS_TYPE_SET_BONUS_TRIGGERS,
    vol.Required("triggers"): [vol.Schema({
        vol.Optional("task_id"): str,
        vol.Optional("wizard_key"): str,
        vol.Required("entity_id"): cv.entity_id,
        vol.Required("to_state"): vol.All(str, vol.Length(min=1, max=255)),
    })],
})
@websocket_api.async_response
async def ws_set_bonus_triggers(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict):
    """Replace the list of bonus triggers (admin only)."""
    if not connection.user.is_admin:
        connection.send_error(msg["id"], "unauthorized", "Admin permission required")
        return
    for trig in msg["triggers"]:
        if not (trig.get("task_id") or trig.get("wizard_key")):
            connection.send_error(msg["id"], "invalid_trigger", "task_id or wizard_key required")
            return
    entry = _get_entry(hass)
    options = dict(entry.options)
    options["bonus_triggers"] = [dict(t) for t in msg["triggers"]]
    hass.config_entries.async_update_entry(entry, options=options)
    connection.send_result(msg["id"], {"success": True})


async def _async_expire_missed(hass: HomeAssistant) -> int:
    """Advance kids' duties that were missed on a previous day - no points, no deduction.

    Runs once per local day (at midnight, and on startup when a midnight was
    missed): every basis duty assigned to a kid that is still due from an
    earlier day is moved to its next occurrence (daily duties right away,
    weekly ones after a grace period). Each one leaves a ``missed`` history
    entry with 0 points. Bonus tasks never expire this way.
    """
    if DOMAIN not in hass.data:
        return 0
    entry = _get_entry(hass)
    if entry.options.get("expire_missed", True) is False:
        return 0
    data = hass.data[DOMAIN]["data"]
    now = dt_util.now()
    today = now.date()
    if data.get("missed_checked") == today.isoformat():
        return 0
    data["missed_checked"] = today.isoformat()

    kids = entry.options.get("kids") or {}
    overdue_days = entry.options.get("overdue_days", 5)
    count = 0
    for task_id, task in list(data["tasks"].items()):
        if not isinstance(task, dict) or normalize_category(task.get("category")) != "basis":
            continue
        kid_ids = [a for a in _assignee_ids(task) if a in kids]
        if not kid_ids:
            continue
        interval = task.get("interval", 7)
        base = interval
        catalog_key, _suffix = kids_catalog.split_key(task.get("wizard_key") or "")
        if task.get("generated_by") == kids_catalog.GENERATED_BY and catalog_key in kids_catalog.CATALOG_BY_KEY:
            base = kids_catalog.CATALOG_BY_KEY[catalog_key]["interval"]
        if not is_missed(task, now, base, overdue_days):
            continue

        due_dt = parse_dt(task["due_date"])
        due_day = (due_dt.astimezone(now.tzinfo) if due_dt.tzinfo else due_dt).date()
        nxt = next_due_day(due_day, interval, today)
        task["due_date"] = dt_util.as_utc(dt_util.start_of_local_day(nxt)).isoformat()
        task["paused_until"] = None
        task.pop("claimed_by", None)
        task.pop("claimed_until", None)
        for st in task.get("subtasks") or []:
            st["done"] = False
        for kid_id in kid_ids:
            data["history"].insert(0, {
                "id": str(uuid.uuid4()),
                "task_id": task_id,
                "task_name": task.get("name") or "",
                "user_id": kid_id,
                "points": 0,
                "timestamp": dt_util.utcnow().isoformat(),
                "type": TYPE_MISSED,
            })
        count += 1
    await hass.data[DOMAIN]["store"].async_save(data)
    if count:
        hass.bus.async_fire(EVENT_TASK_UPDATED)
    return count


def _assignee_ids(task: dict) -> list:
    assignees = task.get("assignees") or []
    return [assignees] if isinstance(assignees, str) else list(assignees)


async def _async_check_monthly_reset(hass: HomeAssistant, force: bool = False):
    """
    Check if a new month has started and perform the points reset.
    
    :param hass: The Home Assistant instance.
    :param force: If True, forces the reset regardless of the current month.
    """
    data = hass.data[DOMAIN]["data"]
    store = hass.data[DOMAIN]["store"]
    current_actual_month = dt_util.now().strftime("%Y-%m")
    saved_month = data.get("current_month")
    
    if saved_month != current_actual_month or force:
        old_points = data["points"].copy()
        
        if saved_month: 
            data["monthly_history"][saved_month] = data["points"].copy()
            
        # Fire monthly reset event with scores before clearing them
        # Resolve names and sort scores for the event payload
        sorted_scores = sorted(old_points.items(), key=lambda item: item[1], reverse=True)
        resolved_scores = []
        person_states = hass.states.async_all("person")
        for u_id, pts in sorted_scores:
            # Resolve friendly name from person entities (with or without HA user)
            name = resolve_participant_name(person_states, u_id)
            resolved_scores.append({
                "user_id": u_id,
                "name": name,
                "points": pts
            })

        if resolved_scores:
            winner = resolved_scores[0]
            hass.bus.async_fire(EVENT_RESET, {
                "month": saved_month or current_actual_month,
                "winner_name": winner["name"],
                "winner_points": winner["points"],
                "all_scores": resolved_scores
            })

        for user in data["points"]: 
            data["points"][user] = 0
            
        data["current_month"] = current_actual_month
        data["current_period_start"] = dt_util.utcnow().isoformat()
        
        _fire_leaderboard_event_if_changed(hass, old_points, data["points"])
        
        await store.async_save(data)
        hass.bus.async_fire(EVENT_TASK_UPDATED)

    _restore_full_goals(hass)


def _restore_full_goals(hass: HomeAssistant) -> None:
    """Replace the prorated goals of the wizard's start month by the full goals.

    Runs on startup and with every monthly check, so it also works when Home
    Assistant was down at the turn of the month.
    """
    entries = hass.config_entries.async_entries(DOMAIN)
    if not entries:
        return
    new_options = restore_full_goals(entries[0].options, dt_util.now().strftime("%Y-%m"))
    if new_options is not None:
        hass.config_entries.async_update_entry(entries[0], options=new_options)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """
    Set up the TaskOrganizer integration from a config entry.
    
    :param hass: The Home Assistant instance.
    :param entry: The configuration entry to setup.
    :return: Boolean indicating successful setup.
    """
    hass.data.setdefault(DOMAIN, {})
    store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
    data = await store.async_load()
    
    if data is None:
        data = {
            "tasks": {}, "points": {}, "history": [], "settings": {}, 
            "monthly_history": {}, "current_month": dt_util.now().strftime("%Y-%m"), 
            "current_period_start": dt_util.utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat(),
            "templates": {}
        }
    
    # Ensure all required keys exist
    data.setdefault("monthly_history", {})
    data.setdefault("current_month", dt_util.now().strftime("%Y-%m"))
    data.setdefault("current_period_start", dt_util.utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat())
    data.setdefault("templates", {})
    # Kids mode step 2: pending approvals and birthday memory
    data.setdefault("pending", {})
    data.setdefault("birthday_handled", {})
    data.setdefault("birthday_levelup", {})

    # Additive migration (kids mode): every task/template gets a valid
    # category ("basis" | "bonus" | "auftrag") and a min_age. Nothing is removed.
    for item in list(data["tasks"].values()) + list(data["templates"].values()):
        if isinstance(item, dict):
            migrate_task_like(item)

    # IMPORTANT: Ensure settings from config entry options are always reflected in data
    data["settings"] = dict(entry.options)

    hass.data[DOMAIN]["store"] = store
    hass.data[DOMAIN]["data"] = data
    # In-memory brute-force protection for the kid PINs
    hass.data[DOMAIN]["pin_limiter"] = PinLimiter(time.monotonic)
    # State listeners for the bonus triggers (device-triggered bonus tasks)
    async_setup_bonus_triggers(hass)

    # Register listener for the OptionsFlow (gear icon)
    entry.async_on_unload(entry.add_update_listener(update_listener))

    # Perform initial check on startup
    await _async_check_monthly_reset(hass)

    async def _timer_check(now):
        """Timer callback to check for month boundaries."""
        await _async_check_monthly_reset(hass)

    # Schedule daily check shortly after midnight
    entry.async_on_unload(async_track_time_change(hass, _timer_check, hour=0, minute=1, second=0))

    async def _adhoc_check(now=None):
        """Remove expired ad-hoc bonus tasks (at least every minute)."""
        await _async_remove_expired_adhoc(hass)

    await _async_remove_expired_adhoc(hass)

    async def _missed_check(now=None):
        """Duties missed on a previous day expire at midnight (and on startup)."""
        await _async_expire_missed(hass)

    # Before the monthly reset / birthday handling; also catches missed midnights on startup
    entry.async_on_unload(async_track_time_change(hass, _missed_check, hour=0, minute=0, second=10))
    await _async_expire_missed(hass)
    entry.async_on_unload(async_track_time_interval(hass, _adhoc_check, timedelta(minutes=1)))

    async def _birthday_check(now=None):
        """Birthday handling (after the monthly reset, so the bonus counts for the new month)."""
        await _async_handle_birthdays(hass)

    entry.async_on_unload(async_track_time_change(hass, _birthday_check, hour=0, minute=2, second=0))
    if hass.is_running:
        await _async_handle_birthdays(hass)
    else:
        # person entities (names) are only available once Home Assistant has started
        async def _birthday_on_start(event=None):
            await _async_handle_birthdays(hass)

        entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _birthday_on_start))

    @callback
    def _mobile_action(event) -> None:
        """Approve / reject from an actionable push notification."""
        action = event.data.get("action")
        if isinstance(action, str) and action.startswith("TO_"):
            hass.async_create_task(_async_handle_push_action(hass, action))

    entry.async_on_unload(hass.bus.async_listen(EVENT_MOBILE_APP_ACTION, _mobile_action))

    # ---------------- SERVICES ----------------

    async def handle_manual_reset(call: ServiceCall): 
        """Service handle to force reset monthly points."""
        await _async_check_monthly_reset(hass, force=True)

    async def handle_factory_reset(call: ServiceCall):
        """Service handle to perform a factory reset."""
        old_points = hass.data[DOMAIN]["data"].get("points", {}).copy()
        
        new_data = {
            "tasks": {}, "points": {}, "history": [], "settings": {}, 
            "monthly_history": {}, "current_month": dt_util.now().strftime("%Y-%m"), 
            "current_period_start": dt_util.utcnow().isoformat(),
            "templates": {}, "pending": {}, "birthday_handled": {}, "birthday_levelup": {},
        }
        hass.data[DOMAIN]["data"] = new_data
        
        _fire_leaderboard_event_if_changed(hass, old_points, new_data["points"])
        
        await store.async_save(new_data)
        hass.bus.async_fire(EVENT_TASK_UPDATED)
        
    async def handle_complete_task_by_name(call: ServiceCall):
        """Service handle to complete a task by its string name."""
        task_name = call.data.get("task_name")
        user_input = call.data.get("user_id")
        target_uid = _get_user_id(hass, user_input) if user_input else call.context.user_id
        
        target_task_id = next(
            (tid for tid, t in data["tasks"].items() if t["name"].lower() == task_name.lower()), 
            None
        )
                
        if not target_task_id:
            _LOGGER.warning("Task '%s' not found.", task_name)
            return
            
        task = data["tasks"][target_task_id]
        completed_by = [target_uid] if target_uid else task.get("assignees", [])
        
        if not completed_by: 
            return
            
        total_points = float(task.get("complexity", 1))
        points_per_user = _calculate_points_per_user(total_points, len(completed_by))
        
        old_points = data["points"].copy()
        undo = _undo_info(task)
        completion_id = str(uuid.uuid4())
        
        for u_id in completed_by:
            if u_id not in data["points"]: 
                data["points"][u_id] = 0
            data["points"][u_id] += points_per_user
            
            history_entry = {
                "id": str(uuid.uuid4()), 
                "task_id": target_task_id,
                "task_name": task.get("name", "Unknown task"),
                "user_id": u_id, 
                "points": points_per_user,
                "timestamp": dt_util.utcnow().isoformat(),
                "type": TYPE_TASK,
                "completion_id": completion_id,
                **copy.deepcopy(undo),
            }
            data["history"].insert(0, history_entry)
        
        interval = task.get("interval", 1)
        task["due_date"] = (dt_util.utcnow() + timedelta(days=interval)).isoformat()
        task["paused_until"] = None 
        task.pop("claimed_by", None)
        task.pop("claimed_until", None)
        
        # Reset subtasks for recurring tasks
        if "subtasks" in task:
            for st in task["subtasks"]:
                st["done"] = False

        # Trigger events
        _fire_leaderboard_event_if_changed(hass, old_points, data["points"])
        hass.bus.async_fire(EVENT_TASK_COMPLETED, {
            "task_id": target_task_id,
            "task_name": task.get("name"),
            "completed_by": completed_by,
            "points_per_user": points_per_user,
            "total_points": total_points
        })
        
        await store.async_save(data)
        hass.bus.async_fire(EVENT_TASK_UPDATED)

    async def handle_add_task(call: ServiceCall):
        """Service handle to create a new task from an automation, with template support."""
        data = hass.data[DOMAIN]["data"]
        store = hass.data[DOMAIN]["store"]
        
        template_name = call.data.get("template")
        template = None
        
        if template_name:
            template = next(
                (t for t in data.get("templates", {}).values() if t["name"].lower() == template_name.lower()),
                None
            )
            if not template:
                _LOGGER.warning("Template '%s' not found.", template_name)
    
        # Use call data, fallback to template data, fallback to default
        name = call.data.get("name") or (template.get("name") if template else None)
        if not name:
            _LOGGER.error("Task name is required when not using a valid template.")
            return
            
        interval = call.data.get("interval")
        if interval is None:
            interval = template.get("interval") if template else 7
            
        assignees_input = call.data.get("assignees")
        if assignees_input is None and template:
            assignees_input = template.get("assignees", [])
        if assignees_input is None:
            assignees_input = []
            
        if isinstance(assignees_input, str):
            assignees_input = [x.strip() for x in assignees_input.split(",")]
            
        assignees = [_get_user_id(hass, assignee) for assignee in assignees_input if _get_user_id(hass, assignee)]
            
        if not assignees and call.context.user_id:
            assignees = [call.context.user_id]
            
        task_id = str(uuid.uuid4())
        new_task_data = {
            "id": task_id, 
            "name": name,
            "description": call.data.get("description") or (template.get("description", "") if template else ""),
            "area": call.data.get("area") or (template.get("area", "") if template else ""),
            "interval": interval,
            "assignees": assignees,
            "complexity": call.data.get("complexity") or (template.get("complexity", 5) if template else 5),
            "category": normalize_category(
                call.data.get("category") or (template.get("category") if template else None)
            ),
            "min_age": normalize_min_age(
                call.data.get("min_age") if call.data.get("min_age") is not None
                else (template.get("min_age", 0) if template else 0)
            ),
            "requires_approval": bool(
                call.data.get("requires_approval") if call.data.get("requires_approval") is not None
                else (template.get("requires_approval", False) if template else False)
            ),
            "requires_routine": bool(
                call.data.get("requires_routine") if call.data.get("requires_routine") is not None
                else (template.get("requires_routine", False) if template else False)
            ),
            "available_from": normalize_time(
                call.data["available_from"] if "available_from" in call.data
                else (template.get("available_from") if template else None)
            ),
            "available_until": normalize_time(
                call.data["available_until"] if "available_until" in call.data
                else (template.get("available_until") if template else None)
            ),
            "icon": call.data.get("icon") or (template.get("icon", "mdi:broom") if template else "mdi:broom"),
            "due_date": dt_util.utcnow().isoformat(), 
            "paused_until": None,
            "override_overdue_days": call.data.get("override_overdue_days"),
            "subtasks": call.data.get("subtasks") or (template.get("subtasks", []) if template else [])
        }
        data["tasks"][task_id] = new_task_data
        hass.bus.async_fire(EVENT_TASK_CREATED, new_task_data)
        await store.async_save(data)
        hass.bus.async_fire(EVENT_TASK_UPDATED)

    async def handle_set_task_due_by_name(call: ServiceCall):
        """Service handle to set a task as due immediately by its name."""
        task_name = call.data.get("task_name")
        
        target_task_id = next(
            (tid for tid, t in data["tasks"].items() if t["name"].lower() == task_name.lower()), 
            None
        )
                
        if not target_task_id:
            _LOGGER.warning("Task '%s' not found for setting due.", task_name)
            return
            
        task = data["tasks"][target_task_id]
        task["due_date"] = dt_util.utcnow().isoformat()
        task["paused_until"] = None
        
        await store.async_save(data)
        hass.bus.async_fire(EVENT_TASK_UPDATED)

    async def handle_pause_task_by_name(call: ServiceCall):
        """Service handle to pause a task by its name."""
        task_name = call.data.get("task_name")
        pause_until = call.data.get("pause_until")
        
        target_task_id = next(
            (tid for tid, t in data["tasks"].items() if t["name"].lower() == task_name.lower()), 
            None
        )
                
        if not target_task_id:
            _LOGGER.warning("Task '%s' not found for pausing.", task_name)
            return

        if not pause_until:
            _LOGGER.warning("Pause until date not provided for task '%s'.", task_name)
            return
            
        task = data["tasks"][target_task_id]
        task["paused_until"] = dt_util.parse_datetime(pause_until).isoformat()

        await store.async_save(data)
        hass.bus.async_fire(EVENT_TASK_UPDATED)

    async def handle_remove_task_by_name(call: ServiceCall):
        """Service handle to remove a task by its name."""
        task_name = call.data.get("task_name")
        
        target_task_id = next(
            (tid for tid, t in data["tasks"].items() if t["name"].lower() == task_name.lower()), 
            None
        )
                
        if not target_task_id:
            _LOGGER.warning("Task '%s' not found for removal.", task_name)
            return
            
        del data["tasks"][target_task_id]
        await store.async_save(data)
        hass.bus.async_fire(EVENT_TASK_UPDATED)

    async def handle_edit_task_by_name(call: ServiceCall):
        """Service handle to edit an existing task by its name."""
        task_name = call.data.get("task_name")
        
        target_task_id = next(
            (tid for tid, t in data["tasks"].items() if t["name"].lower() == task_name.lower()), 
            None
        )
                
        if not target_task_id:
            _LOGGER.warning("Task '%s' not found for editing.", task_name)
            return
            
        task_ref = data["tasks"][target_task_id]
        
        if "new_name" in call.data:
            task_ref["name"] = call.data["new_name"]
        
        keys_to_update = ["description", "area", "interval", "complexity", "icon", "subtasks"]
        for key in keys_to_update:
            if key in call.data: 
                task_ref[key] = call.data[key]

        if "category" in call.data:
            task_ref["category"] = normalize_category(call.data["category"])
        if "min_age" in call.data:
            task_ref["min_age"] = normalize_min_age(call.data["min_age"])
        if "requires_approval" in call.data:
            task_ref["requires_approval"] = bool(call.data["requires_approval"])
        if "requires_routine" in call.data:
            task_ref["requires_routine"] = bool(call.data["requires_routine"])
        for key in ("available_from", "available_until"):
            if key in call.data:
                task_ref[key] = normalize_time(call.data[key])
                
        if "assignees" in call.data:
            assignees_input = call.data["assignees"]
            if isinstance(assignees_input, str):
                assignees_input = [x.strip() for x in assignees_input.split(",")]
            task_ref["assignees"] = [_get_user_id(hass, assignee) for assignee in assignees_input if _get_user_id(hass, assignee)]

        if "override_overdue_days" in call.data:
            task_ref["override_overdue_days"] = call.data["override_overdue_days"]

        await store.async_save(data)
        hass.bus.async_fire(EVENT_TASK_UPDATED)

    async def handle_add_template(call: ServiceCall):
        """Service handle to create a new template."""
        template_id = str(uuid.uuid4())
        
        assignees_input = call.data.get("assignees", [])
        if isinstance(assignees_input, str):
            assignees_input = [x.strip() for x in assignees_input.split(",")]
        assignees = [_get_user_id(hass, assignee) for assignee in assignees_input if _get_user_id(hass, assignee)]
        
        new_template = {
            "id": template_id, 
            "name": call.data.get("name"), 
            "description": call.data.get("description", ""),
            "area": call.data.get("area", ""),
            "complexity": call.data.get("complexity", 5),
            "icon": call.data.get("icon", "mdi:broom"),
            "category": normalize_category(call.data.get("category")),
            "min_age": normalize_min_age(call.data.get("min_age", 0)),
            "requires_approval": bool(call.data.get("requires_approval", False)),
            "requires_routine": bool(call.data.get("requires_routine", False)),
            "available_from": normalize_time(call.data.get("available_from")),
            "available_until": normalize_time(call.data.get("available_until")),
            "interval": call.data.get("interval", 7),
            "assignees": assignees,
            "subtasks": call.data.get("subtasks", [])
        }
        
        data.setdefault("templates", {})[template_id] = new_template
        await store.async_save(data)
        hass.bus.async_fire(EVENT_TASK_UPDATED)

    async def handle_remove_template_by_name(call: ServiceCall):
        """Service handle to remove a template by its name."""
        template_name = call.data.get("template_name")
        
        target_template_id = next(
            (tid for tid, t in data.get("templates", {}).items() if t["name"].lower() == template_name.lower()), 
            None
        )
                
        if not target_template_id:
            _LOGGER.warning("Template '%s' not found for removal.", template_name)
            return
            
        del data["templates"][target_template_id]
        await store.async_save(data)
        hass.bus.async_fire(EVENT_TASK_UPDATED)

    async def handle_edit_template_by_name(call: ServiceCall):
        """Service handle to edit a template by its name."""
        template_name = call.data.get("template_name")
        
        target_template_id = next(
            (tid for tid, t in data.get("templates", {}).items() if t["name"].lower() == template_name.lower()), 
            None
        )
                
        if not target_template_id:
            _LOGGER.warning("Template '%s' not found for editing.", template_name)
            return
            
        template_ref = data["templates"][target_template_id]
        
        if "new_name" in call.data:
            template_ref["name"] = call.data["new_name"]
            
        keys_to_update = ["description", "area", "interval", "complexity", "icon", "subtasks"]
        for key in keys_to_update:
            if key in call.data: 
                template_ref[key] = call.data[key]

        if "category" in call.data:
            template_ref["category"] = normalize_category(call.data["category"])
        if "min_age" in call.data:
            template_ref["min_age"] = normalize_min_age(call.data["min_age"])
        if "requires_approval" in call.data:
            template_ref["requires_approval"] = bool(call.data["requires_approval"])
        if "requires_routine" in call.data:
            template_ref["requires_routine"] = bool(call.data["requires_routine"])
        for key in ("available_from", "available_until"):
            if key in call.data:
                template_ref[key] = normalize_time(call.data[key])
                
        if "assignees" in call.data:
            assignees_input = call.data["assignees"]
            if isinstance(assignees_input, str):
                assignees_input = [x.strip() for x in assignees_input.split(",")]
            template_ref["assignees"] = [_get_user_id(hass, assignee) for assignee in assignees_input if _get_user_id(hass, assignee)]
            
        await store.async_save(data)
        hass.bus.async_fire(EVENT_TASK_UPDATED)

    async def handle_adjust_points(call: ServiceCall):
        """Service handle: give a bonus or a penalty with a reason (kid_id may be a person entity)."""
        # Calls made by a (non-admin) user are refused; automations have no user.
        if call.context.user_id:
            user = await hass.auth.async_get_user(call.context.user_id)
            if user is None or not user.is_admin:
                raise HomeAssistantError("Admin permission required")
        reason = _clean_reason(call.data["reason"])
        if not reason:
            raise ServiceValidationError("A reason is required")
        kid_id = _get_user_id(hass, call.data["kid_id"])
        if not _known_participant(hass, kid_id):
            raise ServiceValidationError(f"Unknown person / participant: {call.data['kid_id']}")
        await _async_adjust_points(hass, kid_id, call.data["points"], reason)

    # Register services
    hass.services.async_register(
        DOMAIN, SERVICE_ADJUST_POINTS, handle_adjust_points,
        schema=vol.Schema({
            vol.Required("kid_id"): cv.string,
            vol.Required("points"): vol.All(vol.Coerce(int), vol.Range(min=-MAX_ADJUST_POINTS, max=MAX_ADJUST_POINTS), vol.NotIn([0])),
            vol.Required("reason"): vol.All(cv.string, vol.Length(max=MAX_REASON_LENGTH)),
        }),
    )
    hass.services.async_register(DOMAIN, SERVICE_RESET_MONTHLY_POINTS, handle_manual_reset)
    hass.services.async_register(DOMAIN, SERVICE_FACTORY_RESET, handle_factory_reset)
    hass.services.async_register(DOMAIN, SERVICE_COMPLETE_TASK_BY_NAME, handle_complete_task_by_name)
    hass.services.async_register(DOMAIN, SERVICE_ADD_TASK, handle_add_task)
    hass.services.async_register(DOMAIN, SERVICE_SET_TASK_DUE_BY_NAME, handle_set_task_due_by_name)
    hass.services.async_register(DOMAIN, SERVICE_PAUSE_TASK_BY_NAME, handle_pause_task_by_name)
    hass.services.async_register(DOMAIN, SERVICE_REMOVE_TASK_BY_NAME, handle_remove_task_by_name)
    hass.services.async_register(DOMAIN, SERVICE_EDIT_TASK_BY_NAME, handle_edit_task_by_name)
    hass.services.async_register(DOMAIN, SERVICE_ADD_TEMPLATE, handle_add_template)
    hass.services.async_register(DOMAIN, SERVICE_REMOVE_TEMPLATE_BY_NAME, handle_remove_template_by_name)
    hass.services.async_register(DOMAIN, SERVICE_EDIT_TEMPLATE_BY_NAME, handle_edit_template_by_name)
    
    # Register websocket API commands
    websocket_api.async_register_command(hass, ws_get_data)
    websocket_api.async_register_command(hass, ws_complete_task)
    websocket_api.async_register_command(hass, ws_add_task)
    websocket_api.async_register_command(hass, ws_edit_task)
    websocket_api.async_register_command(hass, ws_delete_task)
    websocket_api.async_register_command(hass, ws_factory_reset)
    websocket_api.async_register_command(hass, ws_update_settings)
    websocket_api.async_register_command(hass, ws_delete_history_item)
    websocket_api.async_register_command(hass, ws_edit_history_item)
    websocket_api.async_register_command(hass, ws_import_tasks)
    websocket_api.async_register_command(hass, ws_add_template)
    websocket_api.async_register_command(hass, ws_edit_template)
    websocket_api.async_register_command(hass, ws_delete_template)
    websocket_api.async_register_command(hass, ws_set_kid_profile)
    websocket_api.async_register_command(hass, ws_delete_kid_profile)
    websocket_api.async_register_command(hass, ws_kid_verify_pin)
    websocket_api.async_register_command(hass, ws_kid_complete_task)
    websocket_api.async_register_command(hass, ws_kid_claim_task)
    websocket_api.async_register_command(hass, ws_wizard_catalog)
    websocket_api.async_register_command(hass, ws_wizard_preview)
    websocket_api.async_register_command(hass, ws_wizard_apply)
    websocket_api.async_register_command(hass, ws_set_bonus_triggers)
    websocket_api.async_register_command(hass, ws_approve)
    websocket_api.async_register_command(hass, ws_reject)
    websocket_api.async_register_command(hass, ws_adjust_points)
    websocket_api.async_register_command(hass, ws_undo_completion)
    websocket_api.async_register_command(hass, ws_add_adhoc_bonus)
    
    # Register static path for frontend cards
    frontend_path = hass.config.path(f"custom_components/{DOMAIN}/www")
    await hass.http.async_register_static_paths([
        StaticPathConfig("/task_organizer_frontend", frontend_path, False)
    ])

    async def register_lovelace_resources(event=None):
        """Register the custom cards in Lovelace resources."""
        lovelace = hass.data.get("lovelace")
        if not lovelace: 
            return
            
        resources = getattr(lovelace, "resources", None) or lovelace.get("resources")
        if not resources: 
            return
            
        if not resources.loaded: 
            await resources.async_load()
        
        cache_buster = f"{entry.version}_{int(time.time())}"
        base_path = "/task_organizer_frontend"
        needed_cards = [
            f"{base_path}/task-organizer-card.js", 
            f"{base_path}/task-organizer-leaderboard.js", 
            f"{base_path}/task-organizer-stats.js", 
            f"{base_path}/task-organizer-settings.js",
            f"{base_path}/task-organizer-protocol.js",
            f"{base_path}/task-organizer-kids.js",
            f"{base_path}/task-organizer-wizard.js",
            f"{base_path}/task-organizer-parents.js"
        ]
        
        current_resources = {
            (item.get("url") if isinstance(item, dict) else getattr(item, "url", None)): 
            (item.get("id") if isinstance(item, dict) else getattr(item, "id", None)) 
            for item in resources.async_items()
        }
        
        for card_base_url in needed_cards:
            versioned_url = f"{card_base_url}?v={cache_buster}"
            for url, res_id in list(current_resources.items()):
                if url and url.startswith(card_base_url): 
                    await resources.async_delete_item(res_id)
            await resources.async_create_item({"res_type": "module", "url": versioned_url})

    if hass.is_running: 
        hass.async_create_task(register_lovelace_resources())
    else: 
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, register_lovelace_resources)
    
    # Initialize sensors and buttons
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    
    return True

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """
    Unload the TaskOrganizer config entry.
    
    :param hass: The Home Assistant instance.
    :param entry: The configuration entry to unload.
    :return: Boolean indicating successful unload.
    """
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        unsub = hass.data[DOMAIN].pop("bonus_unsub", None)
        if unsub is not None:
            unsub()
        hass.data.pop(DOMAIN)
    return unload_ok