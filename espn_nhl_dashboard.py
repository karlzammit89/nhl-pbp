import streamlit as st
import requests
from datetime import datetime, date as ddate, timedelta, time as dtime
from zoneinfo import ZoneInfo

# =========================
# PAGE CONFIG
# =========================
st.set_page_config(page_title="NHL Live", page_icon="🏒", layout="wide")
st.title("🏒 NHL Dashboard — ESPN")

# =========================
# CONSTANTS
# =========================
ET = ZoneInfo("America/New_York")
ESPN_SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/scoreboard"
ESPN_SUMMARY    = "https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/summary"

SITUATION_MAP = {
    "even":        "5v5",
    "power-play":  "PP",
    "shorthanded": "SH",
    "penalty-shot":"Penalty Shot",
    "empty-net":   "EN",
}

PLAY_EMOJI = {
    "goal":         "🚨",
    "penalty":      "🟡",
    "shot-on-goal": "🎯",
    "blocked-shot": "🛡️",
    "missed-shot":  "🤦",
    "faceoff":      "🏒",
    "hit":          "💥",
    "giveaway":     "❌",
    "takeaway":     "⛹️",
    "stoppage":     "⏸️",
    "period-start": "▶️",
    "period-end":   "⏹️",
    "penalty-shot": "🎯",
}

# =========================
# SESSION STATE
# =========================
for k, v in {
    "view":        "schedule",
    "event_id":    None,
    "away":        "",
    "home":        "",
    "away_logo":   "",
    "home_logo":   "",
    "away_score":  None,
    "home_score":  None,
    "game_state":  "",
    "filters_applied": False,
    "filtered_plays":  None,
    "cached_plays":    None,
    "cached_event_id": None,
}.items():
    if k not in st.session_state:
        st.session_state[k] = v

if "sched_date" not in st.session_state:
    st.session_state.sched_date = ddate.today()

# =========================
# HELPERS
# =========================
def to_et(raw: str):
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(ET)
    except Exception:
        return None

def fmt_et(raw: str) -> str:
    dt = to_et(raw)
    if not dt:
        return "N/A"
    label = "EDT" if dt.dst() != timedelta(0) else "EST"
    return dt.strftime(f"%Y-%m-%d %H:%M:%S {label}")

def fmt_game_time(dt) -> str:
    return dt.strftime("%H:%M ET") if dt else "TBD"

def play_emoji(type_str: str) -> str:
    t = (type_str or "").lower()
    for k, v in PLAY_EMOJI.items():
        if k in t:
            return v
    return "🏒"

def period_label(period_num, period_type: str = "regulation") -> str:
    if "overtime" in (period_type or "").lower() or period_num > 3:
        ot_num = period_num - 3
        return f"OT{ot_num}" if ot_num > 1 else "OT"
    if "shootout" in (period_type or "").lower():
        return "SO"
    return f"P{period_num}"

# =========================
# CACHED API CALLS
# =========================
@st.cache_data(ttl=30, show_spinner=False)
def fetch_scoreboard(date_str: str) -> list:
    date_compact = date_str.replace("-", "")
    try:
        resp = requests.get(
            ESPN_SCOREBOARD,
            params={"dates": date_compact, "limit": 20},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        st.error(f"ESPN error: {e}")
        return []

    games = []
    for event in data.get("events", []):
        comps = event.get("competitions", [{}])
        comp  = comps[0] if comps else {}
        status = comp.get("status", {})
        state  = status.get("type", {}).get("state", "pre")
        state_name = status.get("type", {}).get("shortDetail", "")
        period_num = status.get("period", 0) or 0
        period_type = comp.get("situation", {}).get("lastPlay", {}).get("period", {}).get("type", "")

        competitors = comp.get("competitors", [])
        away = home = {}
        for c in competitors:
            if c.get("homeAway") == "away":
                away = c
            else:
                home = c

        away_score = int(away.get("score", 0) or 0)
        home_score = int(home.get("score", 0) or 0)
        away_team  = away.get("team", {})
        home_team  = home.get("team", {})

        start_str = event.get("date", "")
        start_dt  = to_et(start_str)

        is_final   = state == "post"
        is_live    = state == "in"
        is_ot      = period_num > 3 and (is_final or is_live)
        is_so      = "shootout" in (state_name or "").lower() and (is_final or is_live)

        games.append({
            "event_id":  event.get("id", ""),
            "name":      event.get("name", ""),
            "state":     state,
            "state_name":state_name,
            "period":    period_num,
            "is_live":   is_live,
            "is_final":  is_final,
            "is_ot":     is_ot,
            "is_so":     is_so,
            "away_abbr": away_team.get("abbreviation", "?"),
            "home_abbr": home_team.get("abbreviation", "?"),
            "away_logo": away_team.get("logo", ""),
            "home_logo": home_team.get("logo", ""),
            "away_score":away_score,
            "home_score":home_score,
            "has_score": is_live or is_final,
            "time_str":  fmt_game_time(start_dt),
        })

    return sorted(games, key=lambda x: x["time_str"])


def fetch_plays(event_id: str) -> list:
    """Fetch play-by-play — NOT cached so live games stay fresh."""
    if st.session_state.cached_event_id == event_id and st.session_state.cached_plays is not None:
        return st.session_state.cached_plays

    try:
        resp = requests.get(
            ESPN_SUMMARY,
            params={"event": event_id},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        st.error(f"ESPN error: {e}")
        return []

    raw_plays = data.get("plays", [])
    plays = []
    for p in raw_plays:
        period_obj = p.get("period", {})
        pnum  = period_obj.get("number", 1) if isinstance(period_obj, dict) else 1
        ptype = period_obj.get("type", "") if isinstance(period_obj, dict) else ""

        clock_obj = p.get("clock", {})
        clock_val = clock_obj.get("displayValue", "") if isinstance(clock_obj, dict) else str(clock_obj)

        type_obj   = p.get("type", {})
        type_text  = type_obj.get("text", "") if isinstance(type_obj, dict) else str(type_obj)
        type_abbr  = type_obj.get("abbreviation", "").lower() if isinstance(type_obj, dict) else ""

        wall_raw   = p.get("wallclock", "")
        wall_et    = fmt_et(wall_raw)
        wall_dt    = to_et(wall_raw)

        text       = p.get("text", "")
        seq        = p.get("sequenceNumber", 0)
        try:
            seq = int(seq)
        except Exception:
            seq = 0

        # Situation (PP/EN/SH etc.)
        sit_obj  = p.get("situation", {}) or {}
        strength = sit_obj.get("homeAway", "") or ""
        sit_str  = SITUATION_MAP.get(strength, "")

        is_goal    = "goal" in type_abbr or "goal" in type_text.lower()
        is_penalty = "penalty" in type_abbr or "penalty" in type_text.lower()

        plays.append({
            "id":          str(p.get("id", seq)),
            "seq":         seq,
            "period_num":  pnum,
            "period_type": ptype,
            "period_label":period_label(pnum, ptype),
            "clock":       clock_val,
            "type_text":   type_text,
            "type_abbr":   type_abbr,
            "text":        text,
            "wall_raw":    wall_raw,
            "wall_et":     wall_et,
            "wall_dt":     wall_dt,
            "situation":   sit_str,
            "away_score":  p.get("awayScore", ""),
            "home_score":  p.get("homeScore", ""),
            "is_goal":     is_goal,
            "is_penalty":  is_penalty,
            "emoji":       play_emoji(type_text),
        })

    # Sort by sequence number (ESPN's own ordering)
    plays.sort(key=lambda p: p["seq"])

    st.session_state.cached_plays    = plays
    st.session_state.cached_event_id = event_id
    return plays

# =========================
# SHARED CSS
# =========================
st.markdown("""
<style>
div[data-testid="stVerticalBlockBorderWrapper"] { min-height: 150px; }
.sched-team-row { display:flex; align-items:center; gap:10px; margin-bottom:4px; }
.sched-team-row img { width:34px; height:34px; object-fit:contain; }
.sched-team-name { font-size:22px; font-weight:800; letter-spacing:0.4px; }
.sched-score { font-size:22px; font-weight:800; color:#aaa; margin-left:auto; }
.sched-meta { font-size:13px; color:#999; margin-top:4px;
    border-top:1px solid rgba(255,255,255,0.08); padding-top:5px; }
.badge { display:inline-block; font-size:11px; font-weight:700;
    padding:1px 6px; border-radius:4px; margin-left:6px; vertical-align:middle; }
.badge-ot   { background:#e67e22; color:#fff; }
.badge-live { background:#e74c3c; color:#fff; }
.wall-ok    { color:#2ecc71; font-family:monospace; }
.wall-na    { color:#e67e22; }
</style>
""", unsafe_allow_html=True)

# ======================================================
# GAME FEED VIEW
# ======================================================
if st.session_state.view == "game":

    event_id   = st.session_state.event_id
    away_ab    = st.session_state.away
    home_ab    = st.session_state.home
    away_logo  = st.session_state.away_logo
    home_logo  = st.session_state.home_logo
    away_score = st.session_state.away_score or 0
    home_score = st.session_state.home_score or 0
    gstate     = st.session_state.game_state

    # ── Top buttons ───────────────────────────────────────────────────────
    c1, c2 = st.columns([1, 4])
    with c1:
        if st.button("⬅ Back"):
            st.session_state.view = "schedule"
            st.session_state.cached_plays    = None
            st.session_state.cached_event_id = None
            st.session_state.filtered_plays  = None
            st.session_state.filters_applied = False
            st.rerun()
    with c2:
        if st.button("🔄 Refresh"):
            st.session_state.cached_plays    = None
            st.session_state.cached_event_id = None
            st.session_state.filtered_plays  = None
            st.session_state.filters_applied = False
            st.rerun()

    # ── Header ────────────────────────────────────────────────────────────
    state_label = {"in": "🔴 LIVE", "post": "✅ FINAL", "pre": "🕒 Scheduled"}.get(gstate, gstate)

    h1, h2, h3 = st.columns([1, 6, 1])
    with h1:
        if away_logo:
            st.image(away_logo, width=60)
    with h2:
        st.markdown(
            f"""<div style="display:flex;align-items:center;justify-content:center;
                font-weight:700;font-size:clamp(16px,2.6vw,28px);gap:10px;
                flex-wrap:wrap;text-align:center;">
                <span>{away_ab}</span>
                <span style="color:#aaa;">{away_score}</span>
                <span>–</span>
                <span style="color:#aaa;">{home_score}</span>
                <span>{home_ab}</span>
            </div>
            <div style="text-align:center;font-size:13px;color:#888;margin-top:4px;">
                {state_label}
            </div>""",
            unsafe_allow_html=True,
        )
    with h3:
        if home_logo:
            st.image(home_logo, width=60)

    st.divider()

    # ── Load plays ────────────────────────────────────────────────────────
    with st.spinner("Loading play-by-play…"):
        plays = fetch_plays(event_id)

    if not plays:
        st.info("No play-by-play data available yet.")
        st.stop()

    st.caption(f"Total plays: **{len(plays)}**  |  Source: ESPN API  |  Wall clocks: native ✅")

    # ── Filters ───────────────────────────────────────────────────────────
    all_wall_dts  = [p["wall_dt"] for p in plays if p["wall_dt"]]
    game_start_dt = min(all_wall_dts) if all_wall_dts else None
    game_end_dt   = max(all_wall_dts) if all_wall_dts else None

    all_periods = sorted(
        {p["period_label"] for p in plays},
        key=lambda x: (x.startswith("OT"), x == "SO",
            int(x[1:]) if x.startswith("P") and x[1:].isdigit() else
            int(x[2:]) + 100 if x.startswith("OT") and x[2:].isdigit() else 200)
    )

    USE_PERIOD_FILTER = st.checkbox("🏒 Filter by Period", value=False)
    USE_TIME_FILTER   = st.checkbox("🕐 Filter by Actual Time (ET)", value=False)
    USE_GOAL_FILTER   = st.checkbox("🚨 Goals Only", value=False)
    USE_PENALTY_FILTER= st.checkbox("🟡 Penalties Only", value=False)

    selected_periods = []
    START_DT = END_DT = None

    if USE_PERIOD_FILTER:
        selected_periods = st.multiselect("Select periods", options=all_periods, default=[])

    if USE_TIME_FILTER:
        def_sd   = game_start_dt.date() if game_start_dt else ddate.today()
        def_ed   = game_end_dt.date()   if game_end_dt   else ddate.today()
        def_st   = game_start_dt.time() if game_start_dt else dtime(18, 0)
        def_et_t = game_end_dt.time()   if game_end_dt   else dtime(23, 59)
        st.markdown("**Start date/time (ET)**")
        sc1, sc2 = st.columns(2)
        with sc1:
            start_date = st.date_input("Start date", value=def_sd, key="tf_sd")
        with sc2:
            start_time = st.time_input("Start time", value=def_st, step=60, key="tf_st")
        st.markdown("**End date/time (ET)**")
        ec1, ec2 = st.columns(2)
        with ec1:
            end_date = st.date_input("End date", value=def_ed, key="tf_ed")
        with ec2:
            end_time = st.time_input("End time", value=def_et_t, step=60, key="tf_et")
        START_DT = datetime.combine(start_date, start_time).replace(tzinfo=ET)
        END_DT   = datetime.combine(end_date,   end_time).replace(tzinfo=ET)

    if st.button("🚀 Apply Filters"):
        def passes(p):
            if USE_PERIOD_FILTER and selected_periods and p["period_label"] not in selected_periods:
                return False
            if USE_TIME_FILTER and START_DT and END_DT:
                if not p["wall_dt"] or not (START_DT <= p["wall_dt"] <= END_DT):
                    return False
            if USE_GOAL_FILTER and not p["is_goal"]:
                return False
            if USE_PENALTY_FILTER and not p["is_penalty"]:
                return False
            return True
        st.session_state.filtered_plays  = [p for p in plays if passes(p)]
        st.session_state.filters_applied = True

    filters_applied = st.session_state.filters_applied
    filtered = st.session_state.filtered_plays if filters_applied else plays

    if filters_applied:
        total   = len(plays)
        showing = len(filtered)
        if showing == 0:
            st.warning("⚠️ No results — check your filters.")
            st.stop()
        if USE_PERIOD_FILTER:
            st.info(f"🏒 Period filter: {', '.join(selected_periods or ['none'])} — **{showing}** of **{total}** plays")
        if USE_TIME_FILTER:
            st.info(f"🕐 Time filter: {START_DT.strftime('%H:%M')} → {END_DT.strftime('%H:%M')} ET — **{showing}** of **{total}** plays")
        if USE_GOAL_FILTER:
            st.info(f"🚨 Goals only — **{showing}** of **{total}** plays")
        if USE_PENALTY_FILTER:
            st.info(f"🟡 Penalties only — **{showing}** of **{total}** plays")

    # ── Render plays ──────────────────────────────────────────────────────
    for p in filtered:
        emoji = p["emoji"]
        if p["is_goal"]:
            emoji = "🚨"

        st.subheader(f"{emoji} {p['period_label']} | ⏱️ {p['clock']}")

        st.markdown(f"🎯 **Event:** {p['type_text']}")

        if p["text"]:
            st.markdown(f"📋 {p['text']}")

        score_str = f"{p['away_score']} – {p['home_score']}" if p["away_score"] != "" else "–"
        if p["is_goal"]:
            st.markdown(f"📊 **Score:** {score_str} &nbsp; 🔥 *Goal!*")
        else:
            st.markdown(f"📊 **Score:** {score_str}")

        if p["situation"]:
            st.markdown(f"⚖️ **Situation:** {p['situation']}")

        if p["wall_et"] and p["wall_et"] != "N/A":
            st.success(f"🕐 **Actual Time (ET):** `{p['wall_et']}`")
        else:
            st.caption("🕐 No wall clock on this play")

        st.divider()


# ======================================================
# SCHEDULE VIEW
# ======================================================
else:

    chosen_date = st.date_input(
        "Select date",
        value=st.session_state.sched_date,
        format="YYYY-MM-DD",
    )
    st.session_state.sched_date = chosen_date
    date_str = chosen_date.strftime("%Y-%m-%d")
    st.markdown(f"## NHL Schedule — {date_str}")

    with st.spinner("Loading schedule…"):
        games = fetch_scoreboard(date_str)

    if not games:
        st.info("No games found for this date.")
        st.stop()

    cols = st.columns(2)
    for i, g in enumerate(games):
        away_score_html = f'<span class="sched-score">{g["away_score"]}</span>' if g["has_score"] else ""
        home_score_html = f'<span class="sched-score">{g["home_score"]}</span>' if g["has_score"] else ""

        badges = ""
        if g["is_so"]:
            badges += '<span class="badge badge-ot">SO</span>'
        elif g["is_ot"]:
            badges += '<span class="badge badge-ot">OT</span>'
        if g["is_live"]:
            badges += '<span class="badge badge-live">LIVE</span>'

        meta_line = (
            f'{g["time_str"]} &middot; {g["state_name"]}{badges}'
            if g["has_score"] else
            f'{g["time_str"]} &middot; Scheduled'
        )

        card = f"""
<div class="sched-team-row">
  <img src="{g['away_logo']}"/>
  <span class="sched-team-name">{g['away_abbr']}</span>{away_score_html}
</div>
<div class="sched-team-row">
  <img src="{g['home_logo']}"/>
  <span class="sched-team-name">{g['home_abbr']}</span>{home_score_html}
</div>
<div class="sched-meta">{meta_line}</div>
"""
        with cols[i % 2]:
            with st.container(border=True):
                st.markdown(card, unsafe_allow_html=True)
                if st.button(
                    f"▶  Open  {g['away_abbr']} @ {g['home_abbr']}",
                    key=f"go_{g['event_id']}",
                    use_container_width=True,
                ):
                    st.session_state.view        = "game"
                    st.session_state.event_id    = g["event_id"]
                    st.session_state.away        = g["away_abbr"]
                    st.session_state.home        = g["home_abbr"]
                    st.session_state.away_logo   = g["away_logo"]
                    st.session_state.home_logo   = g["home_logo"]
                    st.session_state.away_score  = g["away_score"]
                    st.session_state.home_score  = g["home_score"]
                    st.session_state.game_state  = g["state"]
                    st.session_state.cached_plays    = None
                    st.session_state.cached_event_id = None
                    st.session_state.filtered_plays  = None
                    st.session_state.filters_applied = False
                    st.rerun()
