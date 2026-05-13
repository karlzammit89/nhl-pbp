import streamlit as st
import requests
from datetime import datetime, date as ddate, timedelta
from zoneinfo import ZoneInfo

# =========================
# PAGE CONFIG & TITLE
# =========================
st.set_page_config(page_title="NHL Live", page_icon="🏒", layout="wide")
st.title("🏒 NHL Dashboard")

# Monday-first calendar via JS locale override with en-CA for YYYY-MM-DD
st.components.v1.html("""
<script>
(function() {
    const orig = Intl.DateTimeFormat;
    Intl.DateTimeFormat = function(l, o) { 
        return new orig('en-CA', o); 
    };
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
    "away_score": 0, "home_score": 0,
    "filters_applied": False,
    "filtered_plays": None,
    "cached_plays": None,
    "cached_event_id": None,
    "last_refresh": datetime.now(ET),
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
    return dt.strftime("%Y-%m-%d %H:%M:%S ET")

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
# DATA FETCHING
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
        display_status = status.get("type", {}).get("shortDetail", "Scheduled")
        
        competitors = comp.get("competitors", [])
        away = next(c for c in competitors if c.get("homeAway") == "away")
        home = next(c for c in competitors if c.get("homeAway") == "home")
        
        games.append({
            "event_id": event.get("id", ""),
            "state_name": display_status,
            "is_live": (state == "in"),
            "is_final": (state == "post"),
            "is_ot": status.get("period", 0) > 3,
            "away_abbr": away.get("team", {}).get("abbreviation", "?"),
            "home_abbr": home.get("team", {}).get("abbreviation", "?"),
            "away_logo": away.get("team", {}).get("logo", ""),
            "home_logo": home.get("team", {}).get("logo", ""),
            "away_score": int(away.get("score", 0) or 0),
            "home_score": int(home.get("score", 0) or 0),
            "has_score": (state != "pre"),
            "time_str": fmt_game_time(to_et(event.get("date", ""))),
        })
    return sorted(games, key=lambda x: x["time_str"])

def get_parsed_plays(event_id: str) -> list:
    st.session_state.last_refresh = datetime.now(ET)
    if st.session_state.cached_event_id == event_id and st.session_state.cached_plays:
        return st.session_state.cached_plays
    
    resp = requests.get(ESPN_SUMMARY, params={"event": event_id}, timeout=15)
    raw_plays = resp.json().get("plays", [])
    plays = []
    
    for p in raw_plays:
        text = p.get("text", "")
        type_text = (p.get("type", {}).get("text", "") or "").upper()
        sit = p.get("situation", {})
        sit_code = str(sit.get("situationCode", ""))
        
        # --- TIER 1: DECODE SITUATION CODE ---
        away_s, home_s = 5, 5
        away_g_in, home_g_in = True, True
        if len(sit_code) == 4:
            away_g_in = (sit_code[0] == '1')
            away_s    = int(sit_code[1])
            home_s    = int(sit_code[2])
            home_g_in = (sit_code[3] == '1')

        # --- TIER 2 & 3: ROBUST DETECTION (FLAGS & KEYWORDS) ---
        l_text = text.lower()
        is_pp = type_text in ["PPG", "SHG", "POWER PLAY GOAL"] or "power play" in l_text
        is_en = type_text == "ENG" or "empty net" in l_text or not (away_g_in and home_g_in)

        # Logical Tagging
        tags = []
        if is_pp:
            if away_s == home_s: # If code is lagging, infer PP from text
                if any(x in l_text for x in [st.session_state.away.lower(), "away"]): 
                    away_s, home_s = 5, 4
                    tags.append(f"{st.session_state.away} PP")
                else: 
                    home_s, away_s = 5, 4
                    tags.append(f"{st.session_state.home} PP")
            else:
                pp_team = st.session_state.away if away_s > home_s else st.session_state.home
                tags.append(f"{pp_team} PP")
        
        if is_en:
            en_team = st.session_state.away if (not away_g_in or "away" in l_text) else st.session_state.home
            tags.append(f"{en_team} EN")

        # Display Adjustment (6v5 etc)
        adj_away = away_s + (0 if away_g_in else 1)
        adj_home = home_s + (0 if home_g_in else 1)
        strength_base = f"{adj_away}v{adj_home}"
        unique_tags = " (" + ", ".join(sorted(set(tags))) + ")" if tags else ""

        plays.append({
            "seq": int(p.get("sequenceNumber", 0)),
            "period_label": period_label(p.get("period", {}).get("number", 1), p.get("period", {}).get("type", "")),
            "clock": p.get("clock", {}).get("displayValue", ""),
            "type_text": type_text,
            "text": text,
            "strength": f"{strength_base}{unique_tags}",
            "is_pp": is_pp or (away_s != home_s),
            "is_en": is_en,
            "wall_et": fmt_et_full(p.get("wallclock", "")),
            "away_score": p.get("awayScore", ""),
            "home_score": p.get("homeScore", ""),
            "emoji": get_play_emoji(text),
        })
    
    plays.sort(key=lambda x: x["seq"], reverse=True)
    st.session_state.cached_plays = plays
    st.session_state.cached_event_id = event_id
    return plays

# ======================================================
# VIEWS
# ======================================================
if st.session_state.view == "game":
    plays = get_parsed_plays(st.session_state.event_id)
    
    nav_col1, nav_col2, nav_col3, _ = st.columns([1.5, 1, 2, 5])
    with nav_col1:
        if st.button("⬅ Back to Schedule"):
            st.session_state.view = "schedule"
            st.rerun()
    with nav_col2:
        if st.button("🔄 Refresh"):
            st.session_state.cached_plays = None
            st.rerun()
    with nav_col3:
        st.success(f"Last Refresh: {st.session_state.last_refresh.strftime('%H:%M:%S ET')}")

    # Header Score
    st.divider()
    h1, h2, h3 = st.columns([1, 4, 1])
    with h1: st.image(st.session_state.away_logo, width=80)
    with h2: st.markdown(f"<h1 style='text-align:center;'>{st.session_state.away} {st.session_state.away_score} - {st.session_state.home_score} {st.session_state.home}</h1>", unsafe_allow_html=True)
    with h3: st.image(st.session_state.home_logo, width=80)
    st.divider()

    # Robust Filtering
    f1, f2, f3 = st.columns(3)
    with f1: USE_GOAL_FILTER = st.checkbox("🚨 Goals Only", value=False)
    with f2: USE_PP_FILTER = st.checkbox("🏒 Power Plays", value=False)
    with f3: USE_GP_FILTER = st.checkbox("🥅 Goalie Pulled (EN)", value=False)

    if st.button("🚀 Apply Filters"):
        def passes(p):
            if USE_GOAL_FILTER and "GOAL" not in p["type_text"]: return False
            if USE_PP_FILTER and not p["is_pp"]: return False
            if USE_GP_FILTER and not p["is_en"]: return False
            return True
        st.session_state.filtered_plays = [p for p in plays if passes(p)]
        st.session_state.filters_applied = True
        st.rerun()

    display_list = st.session_state.filtered_plays if st.session_state.filters_applied else plays
    for p in display_list:
        with st.expander(f"{p['emoji']} {p['period_label']} - {p['clock']} | {p['type_text']} ({p['strength']})", expanded=True):
            st.write(f"**Score:** {st.session_state.away} {p['away_score']} - {p['home_score']} {st.session_state.home}")
            st.write(f"**Play:** {p['text']}")
            st.caption(f"Time: {p['wall_et']}")

else:
    # Schedule View
    date = st.date_input("Select date", value=st.session_state.sched_date)
    games = fetch_scoreboard(date.strftime("%Y-%m-%d"))

    if not games:
        st.info("No games scheduled.")
    else:
        for g in games:
            with st.container(border=True):
                c1, c2, c3, c4 = st.columns([1, 2, 1, 2])
                with c1: st.image(g['away_logo'], width=40)
                with c2: st.write(f"**{g['away_abbr']}** ({g['away_score']})")
                with c3: st.image(g['home_logo'], width=40)
                with c4: st.write(f"**{g['home_abbr']}** ({g['home_score']})")
                
                label = f"Open Game ({g['state_name']})" if g['has_score'] else f"Starts at {g['time_str']}"
                if st.button(label, key=g['event_id'], disabled=not g['has_score'], use_container_width=True):
                    st.session_state.update({
                        "view": "game", "event_id": g["event_id"],
                        "away": g["away_abbr"], "home": g["home_abbr"],
                        "away_logo": g["away_logo"], "home_logo": g["home_logo"],
                        "away_score": g["away_score"], "home_score": g["home_score"],
                        "cached_plays": None
                    })
                    st.rerun()
