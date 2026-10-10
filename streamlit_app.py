from __future__ import annotations

import json
import math
import re
import time
import uuid
from pathlib import Path
from typing import Any

import requests
import streamlit as st


ROOT = Path(__file__).resolve().parent
GAME_PATH = ROOT / "index.html"
GAME_COMPONENT_KEY = "trading_simulator"
REQUEST_TIMEOUT = 12
LEADERBOARD_PAGE_SIZE = 1000
USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,20}$")
SYMBOL_RE = re.compile(r"^[A-Z.]{1,10}$")

st.set_page_config(
    page_title="NYSE Floor Simulator",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)


def _settings() -> tuple[str, str, str]:
    try:
        supabase = st.secrets["supabase"]
        url = str(supabase["url"]).rstrip("/")
        service_key = str(supabase.get("secret_key") or supabase.get("service_role_key") or "")
        anon_key = str(supabase["anon_key"])
        if (
            not re.fullmatch(r"https://[A-Za-z0-9.-]+", url)
            or not (service_key.startswith("sb_secret_") or service_key.startswith("eyJ"))
            or not (anon_key.startswith("sb_publishable_") or anon_key.startswith("eyJ"))
        ):
            raise ValueError("Supabase URL and keys must be set to valid project values.")
        return url, service_key, anon_key
    except (FileNotFoundError, KeyError, ValueError) as exc:
        st.error(
            "App setup is incomplete. Copy `.streamlit/secrets.toml.example` to "
            "`.streamlit/secrets.toml` and add your Supabase project URL and API keys, "
            "then run the SQL in `supabase_schema.sql`."
        )
        st.caption(str(exc))
        st.stop()


SUPABASE_URL, SUPABASE_SERVICE_KEY, SUPABASE_ANON_KEY = _settings()


def _supabase_headers(api_key: str) -> dict[str, str]:
    headers = {"apikey": api_key}
    if api_key.startswith("eyJ"):
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


class SupabaseAuthError(RuntimeError):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


def _auth_request(
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    access_token: str | None = None,
) -> dict[str, Any]:
    headers = {**_supabase_headers(SUPABASE_ANON_KEY), "Content-Type": "application/json"}
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    try:
        response = requests.request(
            method,
            f"{SUPABASE_URL}{path}",
            json=body,
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise RuntimeError("Could not connect to Supabase authentication. Check your connection and try again.") from exc
    try:
        result = response.json() if response.content else {}
    except ValueError:
        result = {}
    if not response.ok:
        message = (
            result.get("msg")
            or result.get("message")
            or result.get("error_description")
            or result.get("error")
            or f"HTTP {response.status_code}"
        )
        raise SupabaseAuthError(response.status_code, str(message)[:300])
    if not isinstance(result, dict):
        raise RuntimeError("Supabase returned an unexpected authentication response.")
    return result


def _store_auth_session(result: dict[str, Any]) -> None:
    access_token = result.get("access_token")
    refresh_token = result.get("refresh_token")
    if not isinstance(access_token, str) or not isinstance(refresh_token, str):
        raise ValueError("Supabase did not return a login session.")
    expires_in = int(result.get("expires_in") or 3600)
    st.session_state["supabase_auth"] = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_at": int(time.time()) + expires_in,
    }


def _authenticated_user() -> dict[str, Any] | None:
    session = st.session_state.get("supabase_auth")
    if not isinstance(session, dict):
        return None
    try:
        if int(session.get("expires_at") or 0) <= int(time.time()) + 60:
            refreshed = _auth_request(
                "POST",
                "/auth/v1/token?grant_type=refresh_token",
                body={"refresh_token": session.get("refresh_token", "")},
            )
            _store_auth_session(refreshed)
            session = st.session_state["supabase_auth"]
        user = _auth_request(
            "GET",
            "/auth/v1/user",
            access_token=session["access_token"],
        )
    except SupabaseAuthError as exc:
        st.session_state.pop("supabase_auth", None)
        if exc.status_code == 401:
            st.session_state["auth_notice"] = "Your sign-in expired. Please sign in again."
            return None
        st.session_state["auth_error"] = str(exc)
        return None
    except (KeyError, TypeError, ValueError, RuntimeError) as exc:
        st.session_state["auth_error"] = str(exc)
        return None
    st.session_state.pop("auth_error", None)
    return user


def _sign_out() -> None:
    session = st.session_state.get("supabase_auth")
    if isinstance(session, dict) and session.get("access_token"):
        try:
            _auth_request(
                "POST",
                "/auth/v1/logout",
                access_token=session["access_token"],
            )
        except (SupabaseAuthError, RuntimeError):
            pass
    st.session_state.pop("supabase_auth", None)
    st.session_state["play_as_guest"] = False


def _user_identity(user: dict[str, Any]) -> dict[str, str]:
    subject = str(user.get("id", ""))
    if not subject or not re.fullmatch(r"[A-Za-z0-9._:-]{1,255}", subject):
        raise ValueError("Supabase did not return a valid user ID.")
    metadata = user.get("user_metadata") if isinstance(user.get("user_metadata"), dict) else {}
    email = str(user.get("email") or "")
    return {
        "sub": subject,
        "name": str(metadata.get("full_name") or metadata.get("name") or (email.split("@")[0] if email else "Trader"))[:120],
        "picture": str(metadata.get("avatar_url") or metadata.get("picture") or "")[:2048],
        "email_verified": str(user.get("email_confirmed_at") or "").lower(),
        "email": email,
    }


def _rest(
    method: str,
    table: str,
    *,
    params: dict[str, str] | None = None,
    body: dict[str, Any] | list[dict[str, Any]] | None = None,
    headers: dict[str, str] | None = None,
) -> Any:
    request_headers = {**_supabase_headers(SUPABASE_SERVICE_KEY), "Content-Type": "application/json"}
    if headers:
        request_headers.update(headers)
    response = requests.request(
        method,
        f"{SUPABASE_URL}/rest/v1/{table}",
        params=params,
        json=body,
        headers=request_headers,
        timeout=REQUEST_TIMEOUT,
    )
    if not response.ok:
        detail = response.text[:500] or f"HTTP {response.status_code}"
        raise RuntimeError(f"Supabase {table} request failed: {detail}")
    if response.status_code == 204 or not response.content:
        return None
    return response.json()


def _profile(user_id: str) -> dict[str, Any] | None:
    rows = _rest(
        "GET",
        "profiles",
        params={"select": "*", "user_id": f"eq.{user_id}", "limit": "1"},
    )
    return rows[0] if rows else None


def _ensure_profile(identity: dict[str, str]) -> dict[str, Any]:
    user_id = identity["sub"]
    profile = _profile(user_id)
    if profile:
        return profile
    username = "trader_" + uuid.uuid5(uuid.NAMESPACE_URL, user_id).hex[:10]
    profile = {
        "user_id": user_id,
        "username": username,
        "display_name": identity["name"],
        "avatar_url": identity["picture"] or None,
        "equity": 25000,
        "peak_equity": 25000,
        "career_day": 1,
        "current_rank": "Junior Floor Trader",
        "experience_mode": "beginner",
        "settings": {},
        "badges": [],
        "last_close": {},
    }
    _rest(
        "POST",
        "profiles",
        params={"on_conflict": "user_id"},
        body=profile,
        headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
    )
    return _profile(user_id) or profile


def _load_trades(user_id: str) -> list[dict[str, Any]]:
    return _rest(
        "GET",
        "trades",
        params={
            "select": "client_trade_id,symbol,side,qty,entry_price,exit_price,net_pnl,mae,mfe,journal_notes,tags,trade_day,exit_time",
            "user_id": f"eq.{user_id}",
            "order": "id.asc",
        },
    )


@st.cache_data(ttl=10, max_entries=2)
def _load_leaderboard(url: str, service_key: str) -> list[dict[str, Any]]:
    headers = _supabase_headers(service_key)
    rows: list[dict[str, Any]] = []
    start = 0
    while True:
        response = requests.get(
            f"{url}/rest/v1/profiles",
            params={
                "select": "user_id,username,display_name,equity,current_rank",
                "order": "equity.desc,username.asc",
                "limit": str(LEADERBOARD_PAGE_SIZE),
                "offset": str(start),
            },
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        )
        if not response.ok:
            raise RuntimeError(f"Could not load the live leaderboard: {response.text[:500]}")
        batch = response.json()
        rows.extend(batch)
        if len(batch) < LEADERBOARD_PAGE_SIZE:
            break
        start += LEADERBOARD_PAGE_SIZE
    return rows


def _public_leaderboard(rows: list[dict[str, Any]], user_id: str) -> list[dict[str, Any]]:
    board: list[dict[str, Any]] = []
    for rank, row in enumerate(rows, start=1):
        board.append(
            {
                "rank": rank,
                "username": row.get("username") or "trader",
                "display_name": row.get("display_name") or "Trader",
                "equity": float(row.get("equity") or 0),
                "current_rank": row.get("current_rank") or "Trader",
                "you": row.get("user_id") == user_id,
            }
        )
    return board


def _number(value: Any, *, low: float, high: float, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a number.") from exc
    if not math.isfinite(number) or not low <= number <= high:
        raise ValueError(f"{label} is outside its allowed range.")
    return number


def _validate_profile(event: dict[str, Any], existing: dict[str, Any], identity: dict[str, str]) -> dict[str, Any]:
    source = event.get("profile")
    if not isinstance(source, dict):
        raise ValueError("Profile update was invalid.")
    username = str(source.get("username") or existing["username"]).strip().lower()
    if not USERNAME_RE.fullmatch(username):
        raise ValueError("Username must be 3–20 characters using letters, numbers, or underscores.")
    settings = source.get("settings") or {}
    if not isinstance(settings, dict):
        raise ValueError("Settings data was invalid.")
    pace = _number(settings.get("pace", 0.5), low=0.25, high=4, label="Simulation pace")
    if pace not in (0.25, 0.5, 1, 2, 4):
        raise ValueError("Simulation pace is not supported.")
    layout = "advanced" if settings.get("layout") == "advanced" else "simple"
    badges = source.get("badges") or []
    if not isinstance(badges, list) or len(badges) > 500:
        raise ValueError("Badge data was invalid.")
    clean_badges = [
        {key: str(badge.get(key, ""))[:120] for key in ("id", "name", "icon", "day")}
        for badge in badges
        if isinstance(badge, dict)
    ]
    equity = _number(source.get("equity"), low=0, high=1_000_000_000_000, label="Equity")
    peak = max(
        equity,
        float(existing.get("peak_equity") or 0),
        _number(source.get("peak_equity", equity), low=0, high=1_000_000_000_000, label="Peak equity"),
    )
    current_rank = str(source.get("current_rank") or existing.get("current_rank") or "Trader")[:80]
    career_day = int(_number(source.get("career_day", 1), low=1, high=1_000_000, label="Career day"))
    return {
        "user_id": identity["sub"],
        "username": username,
        "display_name": identity["name"],
        "avatar_url": identity["picture"] or None,
        "equity": round(equity, 2),
        "peak_equity": round(peak, 2),
        "career_day": career_day,
        "current_rank": current_rank,
        "experience_mode": "pro" if source.get("experience_mode") == "pro" else "beginner",
        "settings": {
            "pace": pace,
            "layout": layout,
            "pauseOnImpact": bool(settings.get("pauseOnImpact", True)),
            "soundVolume": _number(settings.get("soundVolume", 0.7), low=0, high=1, label="Sound volume"),
            "soundMuted": bool(settings.get("soundMuted", False)),
        },
        "badges": clean_badges,
        "last_close": source.get("last_close") if isinstance(source.get("last_close"), dict) else {},
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def _save_profile(
    payload: dict[str, Any],
    identity: dict[str, str],
    *,
    allow_peak_reset: bool = False,
) -> None:
    existing = _profile(identity["sub"])
    if not existing:
        existing = _ensure_profile(identity)
    if allow_peak_reset:
        existing = {**existing, "peak_equity": 0}
    profile = _validate_profile(payload, existing, identity)
    if profile["username"].casefold() != str(existing["username"]).casefold():
        matches = _rest(
            "GET",
            "profiles",
            params={
                "select": "user_id",
                "username": f"eq.{profile['username']}",
                "limit": "1",
            },
        )
        if matches and matches[0]["user_id"] != identity["sub"]:
            raise ValueError("That username is already taken. Choose another one.")
    _rest(
        "POST",
        "profiles",
        params={"on_conflict": "user_id"},
        body=profile,
        headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
    )


def _save_trade(event: dict[str, Any], identity: dict[str, str]) -> None:
    trade = event.get("trade")
    if not isinstance(trade, dict):
        raise ValueError("Trade record was invalid.")
    symbol = str(trade.get("sym") or "").upper()
    side = str(trade.get("side") or "").upper()
    if not SYMBOL_RE.fullmatch(symbol) or side not in {"LONG", "SHORT"}:
        raise ValueError("Trade symbol or side was invalid.")
    trade_id = str(trade.get("cloudId") or "")
    try:
        trade_id = str(uuid.UUID(trade_id))
    except ValueError:
        trade_id = (
            str(uuid.uuid5(uuid.NAMESPACE_URL, f"{identity['sub']}:{trade_id}"))
            if trade_id
            else str(uuid.uuid4())
        )
    tags = [str(trade.get("tag") or "")[:120]] if trade.get("tag") else []
    row = {
        "user_id": identity["sub"],
        "client_trade_id": trade_id,
        "symbol": symbol,
        "side": side,
        "qty": _number(trade.get("qty"), low=0.000001, high=1_000_000_000, label="Quantity"),
        "entry_price": _number(trade.get("entry"), low=0.000001, high=1_000_000_000, label="Entry price"),
        "exit_price": _number(trade.get("exit"), low=0.000001, high=1_000_000_000, label="Exit price"),
        "net_pnl": _number(trade.get("pnl"), low=-1_000_000_000_000, high=1_000_000_000_000, label="Net P&L"),
        "mae": _number(trade.get("mae", 0), low=-1_000_000, high=1_000_000, label="MAE"),
        "mfe": _number(trade.get("mfe", 0), low=-1_000_000, high=1_000_000, label="MFE"),
        "journal_notes": str(trade.get("journalNote") or "")[:480],
        "tags": tags,
        "trade_day": int(_number(trade.get("day", 1), low=1, high=1_000_000, label="Trade day")),
        "exit_time": str(trade.get("time") or "")[:32],
    }
    _rest(
        "POST",
        "trades",
        params={"on_conflict": "user_id,client_trade_id"},
        body=row,
        headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
    )
    profile = event.get("profile")
    if isinstance(profile, dict):
        _save_profile({"profile": profile}, identity)


def _render_auth_gate() -> None:
    st.title("NYSE Floor Simulator")
    st.subheader("Sign in or create an account")
    st.write(
        "Use a Supabase account to save your username, career progress, trading journal, "
        "and badges across devices and appear on the live global leaderboard."
    )
    notice = st.session_state.pop("auth_notice", "")
    if notice:
        st.info(notice)
    auth_error = st.session_state.pop("auth_error", "")
    if auth_error:
        st.error(auth_error)

    login_tab, signup_tab = st.tabs(["Sign in", "Sign up"])
    with login_tab:
        with st.form("supabase_login_form"):
            email = st.text_input("Email", key="login_email", autocomplete="email")
            password = st.text_input("Password", type="password", key="login_password")
            submitted = st.form_submit_button("Sign in", type="primary")
        if submitted:
            if not email.strip() or not password:
                st.error("Enter your email and password.")
            else:
                try:
                    result = _auth_request(
                        "POST",
                        "/auth/v1/token?grant_type=password",
                        body={"email": email.strip(), "password": password},
                    )
                    _store_auth_session(result)
                    st.session_state["play_as_guest"] = False
                    st.rerun()
                except (SupabaseAuthError, RuntimeError, ValueError) as exc:
                    st.error(f"Sign-in failed: {exc}")

    with signup_tab:
        with st.form("supabase_signup_form"):
            display_name = st.text_input("Display name", key="signup_name", max_chars=120)
            new_email = st.text_input("Email", key="signup_email", autocomplete="email")
            new_password = st.text_input("Password", type="password", key="signup_password")
            submitted = st.form_submit_button("Create account", type="primary")
        if submitted:
            if not new_email.strip() or not new_password:
                st.error("Enter an email and password.")
            else:
                try:
                    result = _auth_request(
                        "POST",
                        "/auth/v1/signup",
                        body={
                            "email": new_email.strip(),
                            "password": new_password,
                            "data": {"name": display_name.strip() or new_email.split("@")[0]},
                        },
                    )
                    if result.get("access_token") and result.get("refresh_token"):
                        _store_auth_session(result)
                        st.session_state["play_as_guest"] = False
                        st.rerun()
                    st.success(
                        "Account created. Check your email to confirm the address, then use the Sign in tab."
                    )
                except (SupabaseAuthError, RuntimeError, ValueError) as exc:
                    st.error(f"Sign-up failed: {exc}")

    if st.button("Continue as guest", icon=":material/person_outline:"):
        st.session_state["play_as_guest"] = True
        st.rerun()


def _handle_game_event(component_key: str) -> None:
    component_state = st.session_state.get(component_key, {})
    event = component_state.get("game_event") if isinstance(component_state, dict) else None
    if not isinstance(event, dict):
        return
    try:
        if event.get("type") == "guest_login":
            st.session_state["play_as_guest"] = False
            return
        user = _authenticated_user()
        if not user:
            raise ValueError("Please sign in again before saving account data.")
        identity = _user_identity(user)
        event_type = event.get("type")
        if event_type == "profile_save":
            _save_profile(event, identity)
        elif event_type == "trade_save":
            _save_trade(event, identity)
        elif event_type == "career_reset":
            _save_profile(event, identity, allow_peak_reset=True)
            _rest("DELETE", "trades", params={"user_id": f"eq.{identity['sub']}"})
            st.session_state["profile_notice"] = "Career and saved trade history reset."
        elif event_type == "logout":
            _sign_out()
            st.rerun()
        elif event_type == "username_update":
            current = _profile(identity["sub"]) or _ensure_profile(identity)
            username = str(event.get("username") or "")
            if not USERNAME_RE.fullmatch(username):
                raise ValueError("Username must be 3–20 characters using letters, numbers, or underscores.")
            _save_profile(
                {
                    "profile": {
                        "username": username,
                        "equity": current.get("equity", 25000),
                        "peak_equity": current.get("peak_equity", 25000),
                        "career_day": current.get("career_day", 1),
                        "current_rank": current.get("current_rank", "Junior Floor Trader"),
                        "experience_mode": current.get("experience_mode", "beginner"),
                        "settings": current.get("settings", {}),
                        "badges": current.get("badges", []),
                        "last_close": current.get("last_close", {}),
                    }
                },
                identity,
            )
            st.session_state["profile_notice"] = "Username updated."
        else:
            raise ValueError("Unknown simulator event.")
    except (ValueError, RuntimeError, requests.RequestException) as exc:
        st.session_state["profile_notice"] = str(exc)


GAME_HTML = """
<iframe id="simulator-frame" title="NYSE trading simulator"></iframe>
"""

GAME_CSS = """
:host { display: block; width: 100%; }
iframe { display: block; width: 100%; height: 100%; min-height: 880px; border: 0; background: #05070b; }
"""

GAME_JS = """
export default function(component) {
  const { data, parentElement, setTriggerValue } = component;
  let frame = parentElement.querySelector("#simulator-frame");
  if (!frame) {
    frame = document.createElement("iframe");
    frame.id = "simulator-frame";
    frame.title = "NYSE trading simulator";
    parentElement.appendChild(frame);
  }
  frame.style.width = "100%";
  frame.style.height = "1100px";
  frame.style.border = "0";
  frame.style.background = "#05070b";
  frame.__setGameTrigger = setTriggerValue;
  frame.__leaderboardData = data.leaderboard || [];
  const syncGame = () => {
    const game = frame.contentWindow;
    if (!game) return;
    game.__streamlitEmit = (payload) => frame.__setGameTrigger("game_event", payload);
    game.__streamlitLogin = () => frame.__setGameTrigger("game_event", { type: "guest_login" });
    if (typeof game.__streamlitSetLeaderboard === "function") {
      game.__streamlitSetLeaderboard(frame.__leaderboardData);
    }
  };
  if (!frame.dataset.gameLoaded) {
    frame.addEventListener("load", syncGame, { once: true });
    frame.srcdoc = data.html;
    frame.dataset.gameLoaded = "true";
  } else {
    syncGame();
  }
}
"""

GAME_COMPONENT = st.components.v2.component(
    "nyse_floor_simulator",
    html=GAME_HTML,
    css=GAME_CSS,
    js=GAME_JS,
)


def _render_game(
    *,
    game_html: str,
    leaderboard: list[dict[str, Any]],
    component_key: str,
) -> None:
    GAME_COMPONENT(
        key=component_key,
        data={"html": game_html, "leaderboard": leaderboard},
        height=1100,
        on_game_event_change=lambda: _handle_game_event(component_key),
    )


def main() -> None:
    user = _authenticated_user()
    if user:
        st.session_state.pop("login_password", None)
        st.session_state.pop("signup_password", None)
        st.session_state["play_as_guest"] = False
    if not user:
        if not st.session_state.get("play_as_guest"):
            _render_auth_gate()
            st.stop()

        st.title("NYSE Floor Simulator")
        st.caption("Guest play · progress stays in this browser. Sign in to sync your career.")
        if st.button("Sign in or create account to sync", type="primary", icon=":material/login:"):
            st.session_state["play_as_guest"] = False
            st.rerun()
        guest_html = GAME_PATH.read_text(encoding="utf-8")
        guest_html = guest_html.replace(
            "</head>",
            "<script>window.STREAMLIT_GUEST=true;</script></head>",
            1,
        )
        _render_game(
            game_html=guest_html,
            leaderboard=[],
            component_key=f"{GAME_COMPONENT_KEY}_guest",
        )
        st.stop()

    identity = _user_identity(user)
    component_key = f"{GAME_COMPONENT_KEY}_{identity['sub']}"
    profile = _ensure_profile(identity)
    trades = _load_trades(identity["sub"])
    try:
        board_rows = _load_leaderboard(SUPABASE_URL, SUPABASE_SERVICE_KEY)
        leaderboard = _public_leaderboard(board_rows, identity["sub"])
    except (RuntimeError, requests.RequestException) as exc:
        leaderboard = []
        leaderboard_error = str(exc)
    else:
        leaderboard_error = ""

    bootstrap = {
        "profile": profile,
        "trades": trades,
        "leaderboard": leaderboard,
    }
    json_bootstrap = json.dumps(bootstrap, separators=(",", ":")).replace("<", "\\u003c")
    json_identity = json.dumps(identity, separators=(",", ":")).replace("<", "\\u003c")
    json_supabase = json.dumps(
        {"url": SUPABASE_URL, "anonKey": SUPABASE_ANON_KEY},
        separators=(",", ":"),
    ).replace("<", "\\u003c")
    config_script = (
        "<script>window.STREAMLIT_BRIDGE=true;"
        f"window.STREAMLIT_IDENTITY={json_identity};"
        f"window.STREAMLIT_BOOTSTRAP={json_bootstrap};"
        f"window.SUPABASE_CONFIG={json_supabase};</script>"
    )
    html = GAME_PATH.read_text(encoding="utf-8")
    html = html.replace("</head>", config_script + "</head>", 1)

    with st.sidebar:
        st.title("Trader profile")
        st.markdown(f"**@{profile['username']}**")
        st.caption(f"{identity['name']} · {profile.get('current_rank', 'Trader')}")
        st.caption(identity["email"])
        if st.button("Sign out", icon=":material/logout:", width="stretch"):
            _sign_out()
            st.rerun()
        st.divider()
        st.subheader("Live global leaderboard")
        if leaderboard_error:
            st.error(leaderboard_error)
        notice = st.session_state.pop("profile_notice", "")
        if notice:
            if "failed" in notice.lower() or "taken" in notice.lower() or "must be" in notice.lower():
                st.warning(notice)
            else:
                st.success(notice)

        @st.fragment(run_every=15)
        def live_leaderboard() -> None:
            try:
                current = _public_leaderboard(
                    _load_leaderboard(SUPABASE_URL, SUPABASE_SERVICE_KEY),
                    identity["sub"],
                )
                st.caption(f"{len(current)} traders · refreshes every 15 seconds")
                st.dataframe(
                    [
                        {
                            "Rank": row["rank"],
                            "Username": "@" + row["username"] + (" · you" if row["you"] else ""),
                            "Career rank": row["current_rank"],
                            "Equity": row["equity"],
                        }
                        for row in current
                    ],
                    hide_index=True,
                    height=480,
                    width="stretch",
                    column_config={"Equity": st.column_config.NumberColumn(format="$%,.0f")},
                )
            except (RuntimeError, requests.RequestException) as exc:
                st.error(str(exc))

        live_leaderboard()

    st.title("NYSE Floor Simulator")
    st.caption(
        "Practice with synthetic market data. Your profile and career sync to your account; "
        "the global leaderboard updates live for signed-in traders."
    )
    if leaderboard_error:
        st.warning("The global leaderboard is temporarily unavailable.")
    _render_game(
        game_html=html,
        leaderboard=leaderboard,
        component_key=component_key,
    )


if __name__ == "__main__":
    main()
