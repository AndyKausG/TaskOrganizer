"""Integration tests for kids mode 1.8: time windows, undo, prorated goals, ad-hoc bonus,
wizard re-runs, household day and expiry of missed duties."""

from datetime import datetime, timedelta, timezone

import pytest
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.task_organizer import (
    _async_check_monthly_reset,
    _async_expire_missed,
    _async_remove_expired_adhoc,
)
from custom_components.task_organizer.const import DOMAIN

ANNA, BEN, CLARA, DAVID = "person.anna", "person.ben", "person.clara", "person.david"
SAM_ID = "u-sam"
NOON = datetime(2026, 10, 29, 18, 0, tzinfo=timezone.utc)   # 11:00 local (US/Pacific, PDT)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


def _fake_http(hass):
    async def _noop(paths):
        return None

    hass.http = type("FakeHttp", (), {"async_register_static_paths": staticmethod(_noop)})()


@pytest.fixture
async def setup(hass):
    for pid, name in ((ANNA, "Anna"), (BEN, "Ben"), (CLARA, "Clara"), (DAVID, "David")):
        hass.states.async_set(pid, "home", {"friendly_name": name})
    hass.states.async_set("person.sam", "home", {"friendly_name": "Sam", "user_id": SAM_ID})
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={"overdue_days": 5})
    entry.add_to_hass(hass)
    _fake_http(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _call(ws, type_, **kw):
    await ws.send_json({"id": _call.n, "type": type_, **kw})
    _call.n += 1
    return await ws.receive_json()

_call.n = 9000


async def _make_kid(ws, pid=ANNA, pin="1234", birth_year=2016):
    res = await _call(ws, "task_organizer/set_kid_profile", participant_id=pid, avatar="🦊",
                      color="#ff8800", pin=pin, birth_year=birth_year)
    assert res["success"], res


async def _add_task(ws, **kw):
    payload = {"name": "Task", "interval": 1, "complexity": 4, "icon": "mdi:broom", "category": "basis"}
    payload.update(kw)
    res = await _call(ws, "task_organizer/add_task", **payload)
    assert res["success"], res
    return res["result"]["task_id"]


def _data(hass):
    return hass.data[DOMAIN]["data"]


def _collect(hass, event):
    seen = []
    hass.bus.async_listen(event, lambda e: seen.append(e.data))
    return seen


async def _kid_do(ws, type_, task_id, kid=ANNA, pin="1234"):
    return (await _call(ws, f"task_organizer/{type_}", task_id=task_id, kid_id=kid, pin=pin))["result"]


# ---------------------------------------------------------------------------
# time windows
# ---------------------------------------------------------------------------

async def test_window_fields_roundtrip_and_validation(hass, hass_ws_client, setup):
    ws = await hass_ws_client(hass)
    tid = await _add_task(ws, available_from="17:00", available_until="21:00")
    task = _data(hass)["tasks"][tid]
    assert task["available_from"] == "17:00" and task["available_until"] == "21:00"
    assert (await _call(ws, "task_organizer/edit_task", task_id=tid, available_until=""))["success"]
    assert _data(hass)["tasks"][tid]["available_until"] is None
    bad = await _call(ws, "task_organizer/add_task", name="x", interval=1, complexity=1, icon="mdi:x",
                      available_from="25:99")
    assert bad["success"] is False
    # templates and the service
    res = await _call(ws, "task_organizer/add_template", name="Vorlage", complexity=2, icon="mdi:x",
                      available_from="05:00", available_until="10:00")
    template = _data(hass)["templates"][res["result"]["template_id"]]
    assert template["available_from"] == "05:00" and template["available_until"] == "10:00"
    await hass.services.async_call(DOMAIN, "add_task", {"template": "Vorlage"}, blocking=True)
    copied = [t for t in _data(hass)["tasks"].values() if t["name"] == "Vorlage"][0]
    assert copied["available_from"] == "05:00" and copied["available_until"] == "10:00"
    await hass.services.async_call(DOMAIN, "edit_task_by_name",
                                   {"task_name": "Vorlage", "available_from": "06:30", "available_until": ""},
                                   blocking=True)
    assert copied["available_from"] == "06:30" and copied["available_until"] is None
    await hass.services.async_call(DOMAIN, "edit_template_by_name",
                                   {"template_name": "Vorlage", "available_from": ""}, blocking=True)
    assert template["available_from"] is None


async def test_kid_complete_and_claim_respect_window(hass, hass_ws_client, setup, freezer):
    ws = await hass_ws_client(hass)
    freezer.move_to(NOON)                                  # 11:00 local
    await _make_kid(ws)
    evening = await _add_task(ws, assignees=[ANNA], available_from="17:00", available_until="21:00")
    bonus = await _add_task(ws, category="bonus", available_from="17:00")
    res = await _kid_do(ws, "kid_complete_task", evening)
    assert res == {"success": False, "error": "not_yet", "available_from": "17:00"}
    assert (await _kid_do(ws, "kid_claim_task", bonus))["error"] == "not_yet"
    assert _data(hass)["points"].get(ANNA, 0) == 0

    freezer.move_to(NOON + timedelta(hours=7))             # 18:00 local
    res = await _kid_do(ws, "kid_complete_task", evening)
    assert res["success"] is True

    late = await _add_task(ws, assignees=[ANNA], available_from="05:00", available_until="10:00")
    res = await _kid_do(ws, "kid_complete_task", late)
    assert res == {"success": False, "error": "too_late", "available_until": "10:00"}
    # the parents' complete_task is never restricted
    assert (await _call(ws, "task_organizer/complete_task", task_id=late, completed_by=[ANNA]))["success"]


async def test_locked_basis_task_does_not_block_bonus(hass, hass_ws_client, setup, freezer):
    ws = await hass_ws_client(hass)
    freezer.move_to(NOON)
    await _make_kid(ws)
    await _add_task(ws, assignees=[ANNA], available_from="17:00", available_until="21:00")
    bonus = await _add_task(ws, category="bonus")
    # the evening mission is not open yet -> the bonus is not blocked (Premack rule)
    assert (await _kid_do(ws, "kid_claim_task", bonus))["success"] is True
    # inside the window it blocks again
    freezer.move_to(NOON + timedelta(hours=7))
    other = await _add_task(ws, category="bonus")
    assert (await _kid_do(ws, "kid_claim_task", other))["error"] == "basis_open"


# ---------------------------------------------------------------------------
# undo
# ---------------------------------------------------------------------------

async def test_undo_restores_points_and_due_date(hass, hass_ws_client, setup, freezer):
    ws = await hass_ws_client(hass)
    freezer.move_to(NOON)
    subs = [{"title": "a", "done": True}, {"title": "b", "done": True}]
    tid = await _add_task(ws, interval=3, complexity=5, subtasks=subs)
    before = _data(hass)["tasks"][tid]["due_date"]
    assert (await _call(ws, "task_organizer/complete_task", task_id=tid, completed_by=[ANNA]))["success"]
    data = _data(hass)
    assert data["points"][ANNA] == 5.0 and data["tasks"][tid]["due_date"] != before
    entry = data["history"][0]
    assert entry["completion_id"] and entry["previous_due_date"] == before and entry["subtasks"] == subs
    changed = _collect(hass, "task_organizer_updated")

    res = await _call(ws, "task_organizer/undo_completion", history_id=entry["id"])
    assert res["result"]["success"] is True and res["result"]["removed"] == 1
    assert data["points"][ANNA] == 0 and data["history"] == []
    assert data["tasks"][tid]["due_date"] == before
    assert all(not st["done"] for st in data["tasks"][tid]["subtasks"])
    await hass.async_block_till_done()
    assert changed
    # idempotent
    res = await _call(ws, "task_organizer/undo_completion", history_id=entry["id"])
    assert res["result"] == {"success": False, "error": "not_found"}
    assert data["points"][ANNA] == 0


async def test_undo_clamps_points_and_handles_old_entries(hass, hass_ws_client, setup, freezer):
    ws = await hass_ws_client(hass)
    freezer.move_to(NOON)
    tid = await _add_task(ws, complexity=4)
    await _call(ws, "task_organizer/complete_task", task_id=tid, completed_by=[ANNA])
    data = _data(hass)
    entry = data["history"][0]
    # simulate an entry of an older version: no completion_id / undo info
    for key in ("completion_id", "previous_due_date", "previous_paused_until", "subtasks"):
        entry.pop(key, None)
    data["points"][ANNA] = 1.0                          # points were spent / corrected meanwhile
    res = await _call(ws, "task_organizer/undo_completion", history_id=entry["id"])
    assert res["result"]["success"] is True
    assert data["points"][ANNA] == 0                    # clamped at 0
    due = dt_util.parse_datetime(data["tasks"][tid]["due_date"])
    assert abs((due - dt_util.utcnow()).total_seconds()) < 5   # due now


async def test_undo_recreates_one_time_task(hass, hass_ws_client, setup, freezer):
    ws = await hass_ws_client(hass)
    freezer.move_to(NOON)
    tid = await _add_task(ws, interval=0, complexity=3, name="Einmalig",
                          subtasks=[{"title": "x", "done": True}])
    await _call(ws, "task_organizer/complete_task", task_id=tid, completed_by=[ANNA])
    data = _data(hass)
    assert tid not in data["tasks"]
    entry = data["history"][0]
    assert entry["task_snapshot"]["name"] == "Einmalig"
    res = await _call(ws, "task_organizer/undo_completion", history_id=entry["id"])
    assert res["result"]["success"] is True
    task = data["tasks"][tid]
    assert task["name"] == "Einmalig" and task["interval"] == 0 and task["id"] == tid
    assert task["subtasks"] == [{"title": "x", "done": False}]
    assert data["points"][ANNA] == 0


async def test_undo_multi_participant_and_leaderboard_event(hass, hass_ws_client, setup, freezer):
    ws = await hass_ws_client(hass)
    freezer.move_to(NOON)
    tid = await _add_task(ws, complexity=6)
    await _call(ws, "task_organizer/complete_task", task_id=tid, completed_by=[ANNA, SAM_ID])
    data = _data(hass)
    assert data["points"] == {ANNA: 3.0, SAM_ID: 3.0} and len(data["history"]) == 2
    res = await _call(ws, "task_organizer/undo_completion", history_id=data["history"][1]["id"])
    assert res["result"]["removed"] == 2
    assert data["points"] == {ANNA: 0, SAM_ID: 0} and data["history"] == []


async def test_undo_fires_leaderboard_event_when_order_changes(hass, hass_ws_client, setup, freezer):
    ws = await hass_ws_client(hass)
    freezer.move_to(NOON)
    _data(hass)["points"][ANNA] = 3.0
    tid = await _add_task(ws, complexity=5)
    await _call(ws, "task_organizer/complete_task", task_id=tid, completed_by=[SAM_ID])   # Ben leads now
    leaderboard = _collect(hass, "task_organizer_leaderboard_changed")
    await _call(ws, "task_organizer/undo_completion", history_id=_data(hass)["history"][0]["id"])
    await hass.async_block_till_done()
    assert leaderboard and leaderboard[0]["new_leaderboard"][0]["user_id"] == ANNA


async def test_undo_is_admin_only(hass, hass_ws_client, hass_read_only_access_token, setup):
    from custom_components.task_organizer import _async_complete_task_internal

    data = _data(hass)
    data["tasks"]["t1"] = {"id": "t1", "name": "T", "interval": 1, "complexity": 4, "category": "basis",
                           "due_date": dt_util.utcnow().isoformat(), "assignees": [], "subtasks": []}
    await _async_complete_task_internal(hass, "t1", [ANNA])
    entry_id = data["history"][0]["id"]
    reader = await hass_ws_client(hass, hass_read_only_access_token)
    res = await _call(reader, "task_organizer/undo_completion", history_id=entry_id)
    assert res["error"]["code"] == "unauthorized"
    assert data["points"][ANNA] == 4.0 and len(data["history"]) == 1


async def test_undo_ignores_non_task_entries(hass, hass_ws_client, setup):
    ws = await hass_ws_client(hass)
    await _call(ws, "task_organizer/adjust_points", kid_id=ANNA, points=2, reason="super")
    bonus_id = _data(hass)["history"][0]["id"]
    res = await _call(ws, "task_organizer/undo_completion", history_id=bonus_id)
    assert res["result"]["error"] == "not_found" and _data(hass)["points"][ANNA] == 2


# ---------------------------------------------------------------------------
# prorated goals
# ---------------------------------------------------------------------------

BASE_WIZARD = {
    "kids": [{"id": ANNA, "birth_year": 2014, "avatar": "🦊", "color": "#FF7043"},
             {"id": BEN, "birth_year": 2016, "avatar": "🐼", "color": "#42A5F5"}],
    "features": ["school"], "start_date": "2026-10-16", "phase": "start", "language": "de",
}


async def test_wizard_stores_full_goals_and_resets_next_month(hass, hass_ws_client, setup, freezer):
    ws = await hass_ws_client(hass)
    freezer.move_to(NOON)
    preview = (await _call(ws, "task_organizer/wizard_preview", **BASE_WIZARD))["result"]
    assert preview["prorated"] is True and preview["full_goals_from"] == "2026-11-01"
    kid = preview["kids"][0]
    assert kid["goal_full"] > kid["goal"]

    res = await _call(ws, "task_organizer/wizard_apply", **BASE_WIZARD)
    assert res["success"], res
    opts = setup.options
    assert opts["point_goals"][ANNA] == kid["goal"]
    assert opts["point_goals_full"][ANNA] == kid["goal_full"]
    assert opts["point_goals_prorated_month"] == "2026-10"

    # same month: nothing changes
    await _async_check_monthly_reset(hass)
    assert setup.options["point_goals"][ANNA] == kid["goal"]
    # new month: the full goals apply and the marker is gone
    freezer.move_to(datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc))
    await _async_check_monthly_reset(hass)
    await hass.async_block_till_done()
    assert setup.options["point_goals"][ANNA] == kid["goal_full"]
    assert "point_goals_prorated_month" not in setup.options
    # idempotent
    await _async_check_monthly_reset(hass)
    assert setup.options["point_goals"][ANNA] == kid["goal_full"]


async def test_wizard_edited_goal_is_scaled_up_and_manual_edit_overwrites(hass, hass_ws_client, setup, freezer):
    ws = await hass_ws_client(hass)
    freezer.move_to(NOON)
    res = await _call(ws, "task_organizer/wizard_apply", goals={ANNA: 40}, **BASE_WIZARD)
    assert res["success"], res
    assert setup.options["point_goals"][ANNA] == 40
    assert setup.options["point_goals_full"][ANNA] >= 40
    # a manual edit in the settings card overwrites goal and full goal
    res = await _call(ws, "task_organizer/update_settings", settings={
        "point_goals": {ANNA: 99}, "point_goals_full": {ANNA: 99}})
    assert res["success"]
    freezer.move_to(datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc))
    await _async_check_monthly_reset(hass)
    assert setup.options["point_goals"][ANNA] == 99


async def test_unprorated_start_has_no_marker(hass, hass_ws_client, setup, freezer):
    ws = await hass_ws_client(hass)
    freezer.move_to(NOON)
    res = await _call(ws, "task_organizer/wizard_apply", **{**BASE_WIZARD, "start_date": "2026-11-01"})
    assert res["success"], res
    assert not setup.options.get("point_goals_full")
    assert "point_goals_prorated_month" not in setup.options


# ---------------------------------------------------------------------------
# ad-hoc bonus
# ---------------------------------------------------------------------------

async def test_adhoc_bonus_creates_task_and_events(hass, hass_ws_client, setup, freezer):
    ws = await hass_ws_client(hass)
    freezer.move_to(NOON)
    created = _collect(hass, "task_organizer_task_created")
    opened = _collect(hass, "task_organizer_bonus_opened")
    expires = (NOON + timedelta(hours=3)).isoformat()
    res = await _call(ws, "task_organizer/add_adhoc_bonus", name="  Brot holen ", icon="mdi:bread-slice",
                      points=3, minutes=10, expires_at=expires)
    assert res["success"], res
    task = _data(hass)["tasks"][res["result"]["task_id"]]
    assert task["name"] == "Brot holen" and task["category"] == "bonus" and task["interval"] == 0
    assert task["assignees"] == [] and task["complexity"] == 3 and task["adhoc"] is True
    assert task["min_age"] == 0 and task["icon"] == "mdi:bread-slice"
    assert dt_util.parse_datetime(task["expires_at"]) == NOON + timedelta(hours=3)
    await hass.async_block_till_done()
    assert created and created[0]["name"] == "Brot holen"
    assert opened == [{"task_id": task["id"], "task_name": "Brot holen", "points": 3, "adhoc": True}]


async def test_adhoc_bonus_validation_and_admin_only(hass, hass_ws_client, hass_read_only_access_token, setup, freezer):
    ws = await hass_ws_client(hass)
    freezer.move_to(NOON)
    ok = {"name": "Einkauf", "icon": "mdi:cart", "points": 2}

    async def code(**kw):
        res = await _call(ws, "task_organizer/add_adhoc_bonus", **{**ok, **kw})
        return None if res["success"] else res["error"]["code"]

    assert await code(name="   ") == "invalid_name"
    assert await code(name="x" * 61) == "invalid_name"
    assert await code(name="x" * 60) is None
    assert await code(points=0) == "invalid_format"
    assert await code(points=11) == "invalid_format"
    assert await code(icon="cart") == "invalid_icon"
    assert await code(kid_id="person.nobody") == "unknown_person"
    assert await code(expires_at="nonsense") == "invalid_expiry"
    assert await code(expires_at=(NOON - timedelta(minutes=1)).isoformat()) == "invalid_expiry"
    assert await code(min_age=19) == "invalid_format"
    assert len(_data(hass)["tasks"]) == 1                      # only the valid 60-character one



async def test_adhoc_bonus_is_admin_only(hass, hass_ws_client, hass_read_only_access_token, setup):
    reader = await hass_ws_client(hass, hass_read_only_access_token)
    res = await _call(reader, "task_organizer/add_adhoc_bonus", name="Einkauf", icon="mdi:cart", points=2)
    assert res["error"]["code"] == "unauthorized"
    assert _data(hass)["tasks"] == {}


async def test_adhoc_bonus_assignee_restriction(hass, hass_ws_client, setup, freezer):
    ws = await hass_ws_client(hass)
    freezer.move_to(NOON)
    await _make_kid(ws, ANNA)
    await _make_kid(ws, BEN, pin="4321")
    res = await _call(ws, "task_organizer/add_adhoc_bonus", name="Hund", icon="mdi:dog", points=4, kid_id=BEN)
    tid = res["result"]["task_id"]
    assert _data(hass)["tasks"][tid]["assignees"] == [BEN]
    assert (await _kid_do(ws, "kid_claim_task", tid, kid=ANNA))["error"] == "not_eligible"
    assert (await _kid_do(ws, "kid_claim_task", tid, kid=BEN, pin="4321"))["success"] is True
    # a person with a user account is resolved to the user id
    res = await _call(ws, "task_organizer/add_adhoc_bonus", name="Sam", icon="mdi:star", points=1, kid_id="person.sam")
    assert _data(hass)["tasks"][res["result"]["task_id"]]["assignees"] == [SAM_ID]


async def test_adhoc_bonus_expires_without_points_or_history(hass, hass_ws_client, setup, freezer):
    ws = await hass_ws_client(hass)
    freezer.move_to(NOON)
    await _make_kid(ws)
    expires = (NOON + timedelta(minutes=30)).isoformat()
    res = await _call(ws, "task_organizer/add_adhoc_bonus", name="Einkauf", icon="mdi:cart", points=2,
                      expires_at=expires)
    tid = res["result"]["task_id"]
    keep = await _call(ws, "task_organizer/add_adhoc_bonus", name="Ohne Ablauf", icon="mdi:star", points=2)
    assert await _async_remove_expired_adhoc(hass) == 0
    freezer.move_to(NOON + timedelta(minutes=31))
    # expired but not yet removed: a kid can no longer take it
    assert (await _kid_do(ws, "kid_complete_task", tid))["error"] == "not_eligible"
    # the periodic timer (every minute) removes it
    from pytest_homeassistant_custom_component.common import async_fire_time_changed
    async_fire_time_changed(hass, datetime(2035, 1, 1, tzinfo=timezone.utc))
    await hass.async_block_till_done()
    tasks = _data(hass)["tasks"]
    assert tid not in tasks and keep["result"]["task_id"] in tasks
    assert _data(hass)["history"] == [] and _data(hass)["points"].get(ANNA, 0) == 0


# ---------------------------------------------------------------------------
# wizard re-run keeps the day
# ---------------------------------------------------------------------------

WIZ = {
    "kids": [{"id": ANNA, "birth_year": 2014}, {"id": BEN, "birth_year": 2016}, {"id": CLARA, "birth_year": 2017}],
    "features": ["school"], "start_date": "2026-10-29", "phase": "start", "language": "de",
}


def _by_key(hass):
    return {t["wizard_key"]: t for t in _data(hass)["tasks"].values() if t.get("wizard_key")}


async def test_wizard_rerun_updates_in_place(hass, hass_ws_client, setup, freezer):
    ws = await hass_ws_client(hass)
    freezer.move_to(NOON)
    assert (await _call(ws, "task_organizer/wizard_apply", **WIZ))["success"]
    tasks = _by_key(hass)
    deep = tasks["room_deep:person.anna"]
    morning = tasks["morning:person.anna"]
    old_due = {k: t["due_date"] for k, t in tasks.items()}
    ids = {k: t["id"] for k, t in tasks.items()}
    morning["subtasks"][0]["done"] = True
    morning["claimed_by"] = ANNA
    morning["claimed_until"] = (NOON + timedelta(hours=1)).isoformat()
    deep["paused_until"] = "2026-11-03"

    res = await _call(ws, "task_organizer/wizard_apply", points={"morning:person.anna": 5},
                      replace_existing=True, **WIZ)
    assert res["result"]["updated"] > 0 and res["result"]["created"] == 0 and res["result"]["deleted"] == 0
    after = _by_key(hass)
    assert {k: t["id"] for k, t in after.items()} == ids
    assert after["morning:person.anna"]["complexity"] == 5                 # definition updated
    assert after["morning:person.anna"]["available_from"] == "05:00"
    assert after["morning:person.anna"]["subtasks"][0]["done"] is True
    assert after["morning:person.anna"]["claimed_by"] == ANNA
    assert after["room_deep:person.anna"]["paused_until"] == "2026-11-03"
    # rotation membership unchanged -> the turns are not touched
    for key, task in after.items():
        assert task["due_date"] == old_due[key], key


async def test_wizard_rerun_deletes_unselected_and_restaggers_rotation(hass, hass_ws_client, setup, freezer):
    ws = await hass_ws_client(hass)
    freezer.move_to(NOON)
    assert (await _call(ws, "task_organizer/wizard_apply", **WIZ))["success"]
    assert "table_set:person.clara" in _by_key(hass)
    excluded = ["room_deep:person.anna"]
    two = {**WIZ, "kids": WIZ["kids"][:2]}
    res = await _call(ws, "task_organizer/wizard_apply", replace_existing=True, excluded=excluded, **two)
    assert res["result"]["deleted"] > 0
    tasks = _by_key(hass)
    assert "room_deep:person.anna" not in tasks and "table_set:person.clara" not in tasks
    # rotation of two kids: restarted at TOMORROW (not today), one day apart
    tomorrow = dt_util.now().date() + timedelta(days=1)
    local = lambda t: dt_util.as_local(dt_util.parse_datetime(t["due_date"])).date()
    anna, ben = tasks["table_set:person.anna"], tasks["table_set:person.ben"]
    assert anna["interval"] == 2 and ben["interval"] == 2
    assert {local(anna), local(ben)} == {tomorrow, tomorrow + timedelta(days=1)}
    # rotations that did not change keep their turn
    clara_added = {**WIZ}
    res = await _call(ws, "task_organizer/wizard_apply", replace_existing=True, **clara_added)
    assert res["success"]
    assert local(_by_key(hass)["table_set:person.anna"]) == tomorrow


# ---------------------------------------------------------------------------
# household day
# ---------------------------------------------------------------------------

async def test_household_day_for_weekly_tasks(hass, hass_ws_client, setup, freezer):
    ws = await hass_ws_client(hass)
    freezer.move_to(NOON)
    preview = (await _call(ws, "task_organizer/wizard_preview", **WIZ))["result"]    # 2026-10-29 = Thursday
    assert preview["household_day"] == 5 and preview["household_date"] == "2026-10-31"
    due = {t["wizard_key"]: t["due_date"] for k in preview["kids"] for t in k["tasks"]}
    assert due["room_deep:person.anna"] == "2026-10-31"
    assert due["room_deep:person.ben"] == "2026-10-31"
    assert due["morning:person.anna"] == "2026-10-29"                      # daily: start date
    # bi-weekly: alternating by kid
    assert due["bedding:person.anna"] == "2026-10-31" and due["bedding:person.ben"] == "2026-11-07"
    # weekly rotation starts on the household day, one week apart
    assert {due["plants:person.anna"], due["plants:person.ben"]} >= {"2026-10-31", "2026-11-07"}

    sunday = (await _call(ws, "task_organizer/wizard_preview", household_day=6, **WIZ))["result"]
    assert sunday["household_date"] == "2026-11-01"
    same_day = (await _call(ws, "task_organizer/wizard_preview", **{**WIZ, "start_date": "2026-10-31"}))["result"]
    assert same_day["household_date"] == "2026-10-31"                       # on the day itself

    res = await _call(ws, "task_organizer/wizard_apply", household_day=6, **WIZ)
    assert res["success"]
    assert setup.options["wizard"]["household_day"] == 6
    local = dt_util.as_local(dt_util.parse_datetime(_by_key(hass)["room_deep:person.anna"]["due_date"])).date()
    assert local.isoformat() == "2026-11-01"


# ---------------------------------------------------------------------------
# expiry of missed duties
# ---------------------------------------------------------------------------

def _local_start(day):
    return dt_util.as_utc(dt_util.start_of_local_day(day)).isoformat()


async def test_missed_daily_duty_advances_with_history_entry(hass, hass_ws_client, setup, freezer):
    ws = await hass_ws_client(hass)
    freezer.move_to(NOON)
    await _make_kid(ws)
    today = dt_util.now().date()
    daily = await _add_task(ws, assignees=[ANNA], subtasks=[{"title": "a", "done": True}])
    _data(hass)["tasks"][daily]["due_date"] = _local_start(today - timedelta(days=1))
    today_task = await _add_task(ws, assignees=[ANNA], name="Heute")
    _data(hass)["tasks"][today_task]["due_date"] = _local_start(today)
    bonus = await _add_task(ws, category="bonus", assignees=[ANNA], name="Bonus")
    _data(hass)["tasks"][bonus]["due_date"] = _local_start(today - timedelta(days=3))
    parent_task = await _add_task(ws, assignees=[SAM_ID], name="Eltern")          # no kid profile
    _data(hass)["tasks"][parent_task]["due_date"] = _local_start(today - timedelta(days=3))
    _data(hass).pop("missed_checked", None)

    assert await _async_expire_missed(hass) == 1
    tasks = _data(hass)["tasks"]
    assert tasks[daily]["due_date"] == _local_start(today)                # due again today, fresh
    assert tasks[daily]["subtasks"][0]["done"] is False
    assert tasks[today_task]["due_date"] == _local_start(today)           # untouched
    assert tasks[bonus]["due_date"] == _local_start(today - timedelta(days=3))
    assert tasks[parent_task]["due_date"] == _local_start(today - timedelta(days=3))
    (entry,) = _data(hass)["history"]
    assert entry["type"] == "missed" and entry["points"] == 0 and entry["user_id"] == ANNA
    assert entry["task_id"] == daily and _data(hass)["points"].get(ANNA, 0) == 0
    # only once per day
    tasks[daily]["due_date"] = _local_start(today - timedelta(days=1))
    assert await _async_expire_missed(hass) == 0
    assert len(_data(hass)["history"]) == 1


async def test_missed_weekly_grace_and_rotation_and_setting(hass, hass_ws_client, setup, freezer):
    ws = await hass_ws_client(hass)
    freezer.move_to(NOON)
    await _make_kid(ws)
    today = dt_util.now().date()
    weekly = await _add_task(ws, assignees=[ANNA], interval=7)
    rotation = await _add_task(ws, assignees=[ANNA], interval=3, name="Rotation")
    _data(hass)["tasks"][rotation].update({"wizard_key": "table_set:person.anna", "generated_by": "wizard"})

    def prime(days_weekly):
        _data(hass)["tasks"][weekly]["due_date"] = _local_start(today - timedelta(days=days_weekly))
        _data(hass)["tasks"][rotation]["due_date"] = _local_start(today - timedelta(days=1))
        _data(hass).pop("missed_checked", None)

    prime(2)                                                               # still in the grace period
    assert await _async_expire_missed(hass) == 1                           # only the rotation turn
    assert _data(hass)["tasks"][weekly]["due_date"] == _local_start(today - timedelta(days=2))
    assert _data(hass)["tasks"][rotation]["due_date"] == _local_start(today + timedelta(days=2))   # next turn

    prime(3)
    assert await _async_expire_missed(hass) == 2
    assert _data(hass)["tasks"][weekly]["due_date"] == _local_start(today + timedelta(days=4))

    # disabled in the settings
    prime(5)
    await _call(ws, "task_organizer/update_settings", settings={"expire_missed": False})
    assert await _async_expire_missed(hass) == 0


async def test_missed_midnight_timer_dst_and_startup(hass, hass_ws_client, setup, freezer):
    ws = await hass_ws_client(hass)
    # US/Pacific: DST ends on 2026-11-01 (25-hour day)
    freezer.move_to(datetime(2026, 11, 1, 6, 0, tzinfo=timezone.utc))      # Oct 31, 23:00 PDT
    await _make_kid(ws)
    tid = await _add_task(ws, assignees=[ANNA])
    day = dt_util.now().date()
    assert day.isoformat() == "2026-10-31"
    _data(hass)["tasks"][tid]["due_date"] = _local_start(day)
    _data(hass).pop("missed_checked", None)
    assert await _async_expire_missed(hass) == 0                           # due today: not missed

    freezer.move_to(datetime(2026, 11, 1, 7, 0, 30, tzinfo=timezone.utc))  # Nov 1, 00:00:30 PDT
    from pytest_homeassistant_custom_component.common import async_fire_time_changed
    async_fire_time_changed(hass, datetime(2026, 11, 1, 7, 0, 30, tzinfo=timezone.utc))   # the 00:00:10 timer
    await hass.async_block_till_done()
    task = _data(hass)["tasks"][tid]
    assert task["due_date"] == _local_start(dt_util.now().date())          # Nov 1 00:00 PDT = 07:00Z
    assert task["due_date"].startswith("2026-11-01T07:00")
    assert len(_data(hass)["history"]) == 1

    # next day (25-hour day just ended): missed again, exactly one more entry
    freezer.move_to(datetime(2026, 11, 2, 8, 30, tzinfo=timezone.utc))     # Nov 2, 00:30 PST
    assert await _async_expire_missed(hass) == 1
    assert _data(hass)["tasks"][tid]["due_date"].startswith("2026-11-02T08:00")
    assert await _async_expire_missed(hass) == 0
    assert len(_data(hass)["history"]) == 2
