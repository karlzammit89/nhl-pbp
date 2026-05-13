import streamlit as st
import requests
from datetime import datetime, date as ddate, timedelta, time as dtime
from zoneinfo import ZoneInfo

# =========================
# PAGE CONFIG & TITLE
# =========================
st.set_page_config(page_title="NHL Live", page_icon="🏒", layout="wide")
st.title("🏒 NHL Dashboard")

# Monday-first calendar via JS locale override
st.components.v1.html("""
<script>
(function() {
    const orig = Intl.DateTimeFormat;
    Intl.DateTimeFormat = function(l, o) { return new orig('en-GB', o); };
    Intl.DateTimeFormat.supportedLocalesOf = orig.supportedLocalesOf.bind(orig);
})();
</script>
""", height=0)

# =========================
# CONSTANTS
# =========================
ET = ZoneInfo("America/New_York")
ESPN_SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/scoreboard"
ESPN_SUMMARY    = "https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/summary"

SITUATION_MAP = {
    "even": "5v5", "power-play": "PP", "shorthanded": "SH",
    "penalty-shot": "Penalty Shot", "empty-net": "EN",
}

PLAY_EMOJI = {
    "goal": "🚨", "penalty": "🟡", "shot-on-goal": "🎯",
    "blocked-shot": "🛡️", "missed-shot": "🤦", "faceoff": "🏒",
    "hit": "💥", "giveaway": "❌", "takeaway": "⛹️",
    "stoppage": "⏸️", "period-start": "▶️", "period-end": "⏹️",
}

# =========================
# SESSION STATE INIT
# =========================
for k, v in {
    "view": "schedule",
    "event_id": None,
    "away": "", "home": "",
    "away_logo": "", "home_logo": "",
    "away_score": None, "home_score": None,
    "game_state": "",
    "filters_applied": False,
    "filtered_plays": None,
    "cached_plays": None,
    "cached_event_id": None,
    "last_refresh": None,
    "sched_date": ddate.today(),
}.items():
    if k not in st.session_state:
        st.session_state[k] = v

# =========================
# HELPERS
# =========================
def to_et(raw: str):
    if not raw: return None
    try: return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(ET)
    except: return None

def fmt_et_full(raw: str) -> str:
    dt = to_et(raw)
    if not dt: return "N/A"
    label = "EDT" if dt.dst() != timedelta(0) else "EST"
    return dt.strftime(f"%Y-%m-%d %H:%M:%S {label}")

def fmt_game_time(dt) -> str:
    return dt.strftime("%H:%M ET") if dt else "TBD"

def get_play_emoji(type_str: str) -> str:
    t = (type_str or "").lower()
    for k, v in PLAY_EMOJI.items():
        if k in t: return v
    return "🏒"

def period_label(period_num, period_type: str = "regulation") -> str:
    if "overtime" in (period_type or "").lower() or period_num > 3:
        ot_num = period_num - 3
        return f"OT{ot_num}" if ot_num > 1 else "OT"
    if "shootout" in (period_type or "").lower(): return "SO"
    return f"P{period_num}"

# =========================
# CACHED API CALLS
# =========================
@st.cache_data(ttl=30, show_spinner=False)
def fetch_scoreboard(date_str: str) -> list:
    date_compact = date_str.replace("-", "")
    resp = requests.get(ESPN_SCOREBOARD, params={"dates": date_compact, "limit": 25}, timeout=10)
    data = resp.json()
    games = []
    for event in data.get("events", []):
        comp = event.get("competitions", [{}])[0]
        status = comp.get("status", {})
        state = status.get("type", {}).get("state", "pre")
        competitors = comp.get("competitors", [])
        away = next(c for c in competitors if c.get("homeAway") == "away")
        home = next(c for c in competitors if c.get("homeAway") == "home")
        
        start_dt = to_et(event.get("date", ""))
        is_final, is_live = (state == "post"), (state == "in")
        
        games.append({
            "event_id": event.get("id", ""),
            "state": state,
            "state_name": status.get("type", {}).get("shortDetail", ""),
            "is_live": is_live, "is_final": is_final,
            "is_ot": status.get("period", 0) > 3,
            "away_abbr": away.get("team", {}).get("abbreviation", "?"),
            "home_abbr": home.get("team", {}).get("abbreviation", "?"),
            "away_logo": away.get("team", {}).get("logo", ""),
            "home_logo": home.get("team", {}).get("logo", ""),
            "away_score": int(away.get("score", 0) or 0),
            "home_score": int(home.get("score", 0) or 0),
            "has_score": is_live or is_final,
            "time_str": fmt_game_time(start_dt),
        })
    return sorted(games, key=lambda x: x["time_str"])

def get_parsed_plays(event_id: str) -> list:
    if st.session_state.cached_event_id == event_id and st.session_state.cached_plays:
        return st.session_state.cached_plays
    
    resp = requests.get(ESPN_SUMMARY, params={"event": event_id}, timeout=15)
    raw_plays = resp.json().get("plays", [])
    plays = []
    for p in raw_plays:
        p_obj = p.get("period", {})
        pnum = p_obj.get("number", 1)
        ptype = p_obj.get("type", "")
        t_obj = p.get("type", {})
        text = p.get("text", "")
        
        plays.append({
            "seq": int(p.get("sequenceNumber", 0)),
            "period_label": period_label(pnum, ptype),
            "clock": p.get("clock", {}).get("displayValue", ""),
            "type_text": t_obj.get("text", ""),
            "text": text,
            "wall_et": fmt_et_full(p.get("wallclock", "")),
            "wall_dt": to_et(p.get("wallclock", "")),
            "away_score": p.get("awayScore", ""),
            "home_score": p.get("homeScore", ""),
            "is_goal": "goal" in text.lower(),
            "is_penalty": "penalty" in text.lower(),
            "emoji": get_play_emoji(text),
        })
    plays.sort(key=lambda x: x["seq"])
    st.session_state.cached_plays = plays
    st.session_state.cached_event_id = event_id
    return plays

# ======================================================
# GAME FEED VIEW
# ======================================================
if st.session_state.view == "game":
    # Navigation bar
    nav1, nav2, nav3, _ = st.columns([1.3, 1, 1.3, 6.4])
    with nav1:
        if st.button("⬅ Back to Schedule", use_container_width=True):
            st.session_state.view = "schedule"
            st.rerun()
    with nav2:
        if st.button("🔄 Refresh", use_container_width=True):
            st.session_state.cached_plays = None
            st.session_state.last_refresh = datetime.now(ET)
            st.rerun()
    with nav3:
        if st.session_state.last_refresh:
            st.markdown(f'<div style="background-color:#2e7d32;color:white;padding:8px 16px;border-radius:4px;font-size:14px;font-weight:bold;white-space:nowrap;">Last refresh {st.session_state.last_refresh.strftime("%H:%M:%S ET")}</div>', unsafe_allow_html=True)

    plays = get_parsed_plays(st.session_state.event_id)
    
    # Header
    c1, c2, c3 = st.columns([1, 6, 1])
    with c1: st.image(st.session_state.away_logo, width=60)
    with c2:
        st.markdown(f"""<div style="display:flex;align-items:center;justify-content:center;font-weight:700;font-size:clamp(16px,2.6vw,28px);gap:10px;text-align:center;">
            <span>{st.session_state.away}</span><span style="color:#888;">{st.session_state.away_score}</span>
            <span>-</span>
            <span style="color:#888;">{st.session_state.home_score}</span><span>{st.session_state.home}</span>
            </div>""", unsafe_allow_html=True)
    with c3: st.image(st.session_state.home_logo, width=60)
    st.divider()

    # --- Filters ---
    all_periods = sorted(list({p["period_label"] for p in plays}))
    
    USE_PERIOD_FILTER = st.checkbox("🏒 Filter by Period", value=False)
    USE_TIME_FILTER   = st.checkbox("🕐 Filter by Actual Time (ET)", value=False)
    USE_GOAL_FILTER   = st.checkbox("🚨 Goals Only", value=False)

    selected_periods = []
    START_DT = END_DT = None

    if USE_PERIOD_FILTER:
        selected_periods = st.multiselect("Select Periods", options=all_periods, default=[])

    if USE_TIME_FILTER:
        # 1. Get reference timestamps from the play data
        all_wall_dts = [p["wall_dt"] for p in plays if p["wall_dt"]]
        game_start_dt = min(all_wall_dts) if all_wall_dts else datetime.now(ET)
        game_end_dt   = max(all_wall_dts) if all_wall_dts else datetime.now(ET)

        # 2. Start Date/Time Row (Matches image_3a4676.png)
        st.markdown("**Start date/time (ET)**")
        col_sd, col_st = st.columns(2)
        with col_sd:
            start_date = st.date_input("Start date", value=game_start_dt.date(), key="sd")
        with col_st:
            start_time = st.time_input("Start time", value=game_start_dt.time(), key="st")
        
        # 3. End Date/Time Row (Matches image_3a4676.png)
        st.markdown("**End date/time (ET)**")
        col_ed, col_et = st.columns(2)
        with col_ed:
            end_date = st.date_input("End date", value=game_end_dt.date(), key="ed")
        with col_et:
            end_time = st.time_input("End time", value=game_end_dt.time(), key="et")
        
        # 4. Create final localized datetime objects for comparison
        START_DT = datetime.combine(start_date, start_time).replace(tzinfo=ET)
        END_DT   = datetime.combine(end_date, end_time).replace(tzinfo=ET)

    if st.button("🚀 Apply Filters"):
        def passes(p):
            if USE_PERIOD_FILTER and selected_periods:
                if p["period_label"] not in selected_periods:
                    return False
            
            # This now compares full Date + Time objects
            if USE_TIME_FILTER and START_DT and END_DT:
                if not p["wall_dt"] or not (START_DT <= p["wall_dt"] <= END_DT):
                    return False
                    
            if USE_GOAL_FILTER and p["type_text"] != "Goal":
                return False
            return True

        st.session_state.filtered_plays = [p for p in plays if passes(p)]
        st.session_state.filters_applied = True
        
    # Determine which list to render
    filters_applied = st.session_state.filters_applied
    filtered = st.session_state.filtered_plays if (filters_applied and st.session_state.filtered_plays is not None) else plays

    # Display Info Banners (Matching NBA UI)
    if filters_applied:
        total = len(plays)
        showing = len(filtered)
        if showing == 0:
            st.warning("⚠️ No results found — please check the filters applied.")
            st.stop()
        
        if USE_PERIOD_FILTER:
            st.info(f"🏒 **Period filter:** {', '.join(selected_periods)} — showing **{showing}** of **{total}** plays")
        if USE_TIME_FILTER:
            st.info(f"🕐 **Time filter:** {START_DT.strftime('%H:%M')} → {END_DT.strftime('%H:%M')} ET — showing **{showing}** of **{total}** plays")
        if USE_GOAL_FILTER:
            st.info(f"🚨 **Goals Only filter:** showing **{showing}** of **{total}** plays")

    # --- Render loop ---
    for p in filtered:
        # Determine emoji: Use specific goal emoji if it's a goal
        emoji = p["emoji"] if p["type_text"] != "Goal" else "🚨"
        
        st.subheader(f"{emoji} {p['period_label']} | ⏱️ {p['clock']}")
        
        # NEW: Display the Event Type
        st.markdown(f"🎯 **Event:** {p['type_text']}")

        # UPDATED: Description without the 🔥 Goal! text
        st.markdown(f"📋 **Play:** {p['text']}")
            
        # Score and metadata
        st.markdown(f"📊 **Score:** {p['away_score']} - {p['home_score']}")
        
        if "situation" in p and p["situation"]:
            st.markdown(f"⚖️ **Situation:** {p['situation']}")
        
        if p["wall_et"]:
            st.markdown(f"🕐 **Time (ET):** `{p['wall_et']}`")
        
        st.divider()
        
# ======================================================
# SCHEDULE VIEW
# ======================================================
else:
    date = st.date_input("Select date", st.session_state.sched_date)
    st.session_state.sched_date = date
    games = fetch_scoreboard(date.strftime("%Y-%m-%d"))

    st.markdown("""<style>
        .sched-team-row { display: flex; align-items: center; gap: 10px; margin-bottom: 4px; }
        .sched-team-name { font-size: 22px; font-weight: 800; }
        .sched-score { font-size: 22px; font-weight: 800; color: #aaa; margin-left: auto; }
        .sched-meta { font-size: 13px; color: #999; border-top: 1px solid rgba(255,255,255,0.08); padding-top: 5px; }
        .sched-extra { background: #e67e22; color: #fff; font-size: 11px; padding: 1px 6px; border-radius: 4px; margin-left: 6px; }
    </style>""", unsafe_allow_html=True)

    cols = st.columns(2)
    for i, g in enumerate(games):
        has_started = g["has_score"]
        ot_badge = '<span class="sched-extra">OT</span>' if g["is_ot"] else ""
        
        card_html = f"""
        <div class="sched-team-row"><img src="{g['away_logo']}" width="34"/><span class="sched-team-name">{g['away_abbr']}</span><span class="sched-score">{g['away_score'] if has_started else ''}</span></div>
        <div class="sched-team-row"><img src="{g['home_logo']}" width="34"/><span class="sched-team-name">{g['home_abbr']}</span><span class="sched-score">{g['home_score'] if has_started else ''}</span></div>
        <div class="sched-meta">{g['time_str']} &middot; {g['state_name']}{ot_badge}</div>
        """
        with cols[i % 2]:
            with st.container(border=True):
                st.markdown(card_html, unsafe_allow_html=True)
                if st.button(f"▶ Open {g['away_abbr']} @ {g['home_abbr']}" if has_started else "⏳ Not Started", key=f"g_{g['event_id']}", use_container_width=True, disabled=not has_started):
                    st.session_state.update({"view": "game", "event_id": g["event_id"], "away": g["away_abbr"], "home": g["home_abbr"], "away_logo": g["away_logo"], "home_logo": g["home_logo"], "away_score": g["away_score"], "home_score": g["home_score"], "game_state": g["state"]})
                    st.rerun()
