import streamlit as st
import requests
from datetime import datetime, date as ddate, timedelta, time as dtime
from zoneinfo import ZoneInfo

# =========================
# PAGE CONFIG & TITLE
# =========================
st.set_page_config(page_title="NHL Play by Play", page_icon="🏒", layout="wide")
st.title("🏒 NHL Play by Play")

st.components.v1.html("""
<script>
(function() {
    const orig = Intl.DateTimeFormat;
    Intl.DateTimeFormat = function(l, o) { return new orig('en-CA', o); };
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
NHL_SCHEDULE    = "https://api-web.nhle.com/v1/schedule"
NHL_PBP         = "https://api-web.nhle.com/v1/gamecenter"

PLAY_EMOJI = {
    "goal": "🚨", "penalty": "🟡", "shot-on-goal": "🎯",
    "blocked-shot": "🛡️", "missed-shot": "🤦", "faceoff": "🏒",
    "hit": "💥", "giveaway": "❌", "takeaway": "⛹️",
    "stoppage": "⏸️", "period-start": "▶️", "period-end": "⏹️",
}

FUZZY_SECONDS = 2

# =========================
# SESSION STATE INIT
# =========================
for k, v in {
    "view": "schedule",
    "event_id": None, "nhl_game_id": None,
    "away": "", "home": "",
    "away_logo": "", "home_logo": "",
    "away_score": None, "home_score": None,
    "game_state": "",
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
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(ET)
    except Exception:
        return None

def fmt_et_full(raw: str) -> str:
    dt = to_et(raw)
    if not dt:
        return "N/A"
    label = "EDT" if dt.dst() != timedelta(0) else "EST"
    return dt.strftime(f"%Y-%m-%d %H:%M:%S {label}")

def fmt_game_time(dt) -> str:
    return dt.strftime("%H:%M ET") if dt else "TBD"

def get_play_emoji(type_str: str) -> str:
    t = (type_str or "").lower()
    for k, v in PLAY_EMOJI.items():
        if k in t:
            return v
    return "🏒"

def period_label(period_num, period_type: str = "") -> str:
    if "overtime" in (period_type or "").lower() or (isinstance(period_num, int) and period_num > 3):
        ot_num = period_num - 3
        return f"OT{ot_num}" if ot_num > 1 else "OT"
    if "shootout" in (period_type or "").lower():
        return "SO"
    return f"P{period_num}"

def espn_clock_to_seconds(clock_str: str, period_num: int) -> int:
    """
    ESPN NHL clock counts UP from 0:00 (elapsed).
    e.g. "02:23" = 2min 23sec into the period.
    Returns: period_offset + elapsed_in_period
    """
    try:
        parts = clock_str.strip().split(":")
        mins, secs = int(parts[0]), int(parts[1])
        elapsed = mins * 60 + secs
    except Exception:
        elapsed = 0
    return (period_num - 1) * 1200 + elapsed

def nhl_clock_to_seconds(time_in_period: str, period_num: int) -> int:
    """
    NHL clock counts DOWN from 20:00 (time remaining).
    e.g. "14:48" means 5min 12sec elapsed.
    Returns: period_offset + (1200 - remaining_secs)
    """
    try:
        parts = time_in_period.strip().split(":")
        mins, secs = int(parts[0]), int(parts[1])
        elapsed = 1200 - (mins * 60 + secs)
    except Exception:
        elapsed = 0
    return (period_num - 1) * 1200 + elapsed

def parse_nhl_situation(sit_code: str) -> str:
    """
    Parse NHL situation code: [away_goalie][away_sk][home_sk][home_goalie]
    Goalie: '1'=in net, '0'=pulled (EN). Skaters 1-6 (6=extra attacker).
    Returns e.g. '5v5', '4v5 PP', '6v5 Away EN PP', '5v5 Home EN'
    """
    if not sit_code or len(sit_code) < 4:
        return ""
    away_g  = sit_code[0]
    away_sk = sit_code[1]
    home_sk = sit_code[2]
    home_g  = sit_code[3]
    try:
        a = int(away_sk)
        h = int(home_sk)
    except ValueError:
        return ""

    is_away_en = (away_g == "0")
    is_home_en = (home_g == "0")
    is_pp      = (a != h)

    parts = [f"{a}v{h}"]
    if is_away_en and is_home_en:
        parts.append("Both EN")
    elif is_away_en:
        parts.append("Away EN")
    elif is_home_en:
        parts.append("Home EN")
    if is_pp:
        parts.append("PP")

    return " ".join(parts)

# =========================
# NHL GAME ID LOOKUP
# =========================
@st.cache_data(ttl=3600, show_spinner=False)
def find_nhl_game_id(date_str: str, away_abbr: str, home_abbr: str) -> str:
    try:
        resp = requests.get(f"{NHL_SCHEDULE}/{date_str}", timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return ""

    target_date = datetime.fromisoformat(date_str).date()
    for day in data.get("gameWeek", []):
        for g in day.get("games", []):
            start_dt = to_et(g.get("startTimeUTC", ""))
            if start_dt and start_dt.date() != target_date:
                continue
            nhl_away = g.get("awayTeam", {}).get("abbrev", "").upper()
            nhl_home = g.get("homeTeam", {}).get("abbrev", "").upper()
            if _abbr_match(away_abbr, nhl_away) and _abbr_match(home_abbr, nhl_home):
                return str(g.get("id", ""))
    return ""

def _abbr_match(espn_abbr: str, nhl_abbr: str) -> bool:
    espn_abbr = espn_abbr.upper()
    nhl_abbr  = nhl_abbr.upper()
    if espn_abbr == nhl_abbr:
        return True
    KNOWN = {
        "VGK": ["VEG", "VGK", "LV"],
        "VEG": ["VGK", "VEG", "LV"],
        "LV":  ["VGK", "VEG"],
        "UTA": ["UTAH", "UTA"],
        "WSH": ["WAS", "WSH"],
        "WAS": ["WSH", "WAS"],
        "CLB": ["CBJ", "CLB"],
        "CBJ": ["CLB", "CBJ"],
    }
    aliases = KNOWN.get(espn_abbr, []) + KNOWN.get(nhl_abbr, [])
    return espn_abbr in aliases or nhl_abbr in aliases

# =========================
# NHL PLAY-BY-PLAY FETCH
# =========================
@st.cache_data(ttl=30, show_spinner=False)
def fetch_nhl_plays(nhl_game_id: str) -> list:
    if not nhl_game_id:
        return []
    try:
        resp = requests.get(f"{NHL_PBP}/{nhl_game_id}/play-by-play", timeout=10)
        resp.raise_for_status()
        plays = resp.json().get("plays", [])
    except Exception:
        return []

    result = []
    for p in plays:
        pd       = p.get("periodDescriptor", {})
        pnum     = pd.get("number", 1)
        tip      = p.get("timeInPeriod", "20:00")
        sit      = p.get("situationCode") or ""
        type_key = p.get("typeDescKey", "")
        result.append({
            "period":     pnum,
            "elapsed":    nhl_clock_to_seconds(tip, pnum),
            "time_in":    tip,
            "type_key":   type_key,
            "sit_code":   sit,
            "situation":  parse_nhl_situation(sit),
            "sort_order": p.get("sortOrder", 0),
        })
    return result

# =========================
# SITUATION WINDOW BUILDER
# =========================
ESPN_TO_NHL_TYPE = {
    "goal":         ["goal"],
    "penalty":      ["penalty", "delayed-penalty"],
    "shot on goal": ["shot-on-goal"],
    "blocked shot": ["blocked-shot"],
    "missed shot":  ["missed-shot"],
    "faceoff":      ["faceoff"],
    "hit":          ["hit"],
    "giveaway":     ["giveaway"],
    "takeaway":     ["takeaway"],
    "stoppage":     ["stoppage"],
    "period start": ["period-start"],
    "period end":   ["period-end"],
}

def espn_type_to_nhl(espn_type: str) -> list:
    t = (espn_type or "").lower().strip()
    for key, nhl_keys in ESPN_TO_NHL_TYPE.items():
        if key in t:
            return nhl_keys
    return []

def build_situation_windows(nhl_plays: list) -> list:
    """
    Build gapless situation windows from NHL play-by-play data.

    NHL rules embedded in this logic:
    - PP requires a penalty — validated in Phase 4
    - 5-minute majors create up to 300s of PP, even after a goal is scored
      (unlike 2-min minors where scoring ends the PP)
    - EN requires no penalty — team can pull goalie any time
    - Carry-over PPs from previous period are valid

    Four phases:
    1. Build raw gapless windows from every play
    2. Patch period-start boundary windows for carry-over PPs
    3. Merge adjacent same-situation windows (eliminate fragmentation)
    4. Validate PP windows: must have causative penalty within lookback window
       and minimum duration to filter out NHL API data artifacts
    """
    if not nhl_plays:
        return []

    BOUNDARY_TYPES = {
        "period-start", "period-end",
        "game-start",   "game-end",
        "shootout-start", "shootout-end",
    }

    sorted_plays = sorted(
        nhl_plays,
        key=lambda p: (p["period"], p["elapsed"], p["sort_order"])
    )

    # ── Phase 1: raw gapless windows ──────────────────────────────────────
    raw_windows  = []
    prev_sit     = None
    win_start    = 0
    win_sit_code = ""
    win_sit      = ""
    win_boundary = False

    for p in sorted_plays:
        sit = p["sit_code"]
        if sit == prev_sit:
            continue
        if prev_sit is not None:
            raw_windows.append([win_start, p["elapsed"], win_sit_code, win_sit, win_boundary])
        win_start    = p["elapsed"]
        win_sit_code = sit
        win_sit      = p["situation"]
        win_boundary = p.get("type_key", "") in BOUNDARY_TYPES
        prev_sit     = sit

    if prev_sit is not None:
        raw_windows.append([win_start, 99999, win_sit_code, win_sit, win_boundary])

    # ── Phase 2: patch period-start carry-over windows ────────────────────
    # A PP that started in P1 carries into P2. The period-start event logs
    # sit='1551' but the first real PP play follows immediately.
    # Patch the boundary window to show the actual carry-over situation.
    period_starts = {
        p["elapsed"] for p in sorted_plays
        if p.get("type_key", "") == "period-start"
    }
    for i, win in enumerate(raw_windows):
        w_start, w_end, w_sit_code, w_sit, w_is_boundary = win
        if not w_is_boundary or w_start not in period_starts:
            continue
        next_real = next(
            (raw_windows[j] for j in range(i + 1, len(raw_windows))
             if not raw_windows[j][4]),
            None
        )
        if next_real and next_real[2] != w_sit_code and next_real[0] - w_start <= 60:
            raw_windows[i][3] = next_real[3]

    # ── Phase 2b: gap-fill missing PP windows from penalty plays ──────────
    # Root cause: the NHL API sometimes logs a penalty play but then logs
    # ZERO plays with the resulting PP situation code. Phase 1 never sees
    # a sit code change so no PP window is created, even though a real PP
    # occurred on the ice.
    #
    # Fix: for every penalty play, check if a PP window already covers the
    # expected PP timeframe. If not, look for any play logged AFTER the
    # penalty that has a PP situation code and use it to synthesise a window.
    penalty_plays = [
        p for p in sorted_plays
        if p.get("type_key", "") in ("penalty", "delayed-penalty")
    ]
    for pen in penalty_plays:
        pen_e            = pen["elapsed"]
        pp_expected_end  = pen_e + 360   # max 5-min major + buffer

        # Check if any existing raw window already covers this penalty's PP
        covered = any(
            "PP" in w[3]
            and w[0] <= pp_expected_end
            and w[1] >= pen_e
            for w in raw_windows
        )
        if covered:
            continue

        # No PP window exists — find the first play with a PP sit code
        # that occurs after this penalty and within the expected window
        first_pp = next(
            (p for p in sorted_plays
             if p["elapsed"] > pen_e
             and p["elapsed"] <= pp_expected_end
             and "PP" in p["situation"]),
            None
        )
        if not first_pp:
            continue  # truly no PP data — skip

        # Find where the PP ends (first non-PP play after the PP starts)
        synth_end = next(
            (p["elapsed"] for p in sorted_plays
             if p["elapsed"] > first_pp["elapsed"]
             and "PP" not in p["situation"]),
            first_pp["elapsed"] + 120
        )

        # Insert synthesised window — it will be merged in Phase 3
        synth = [first_pp["elapsed"], synth_end,
                 first_pp["sit_code"], first_pp["situation"], False]
        inserted = False
        for i, w in enumerate(raw_windows):
            if w[0] >= synth[0]:
                raw_windows.insert(i, synth)
                inserted = True
                break
        if not inserted:
            raw_windows.append(synth)

    # ── Phase 3: merge adjacent same-situation windows ────────────────────
    merged = []
    for w in raw_windows:
        sit_str = w[3]
        if merged and merged[-1][2] == sit_str:
            merged[-1][1] = w[1]
        else:
            merged.append([w[0], w[1], sit_str])

    # Remove zero-duration windows — unreachable by [start, end) lookup
    merged = [w for w in merged if w[1] > w[0]]

    # ── Phase 4: validate PP windows against NHL rules ─────────────────────
    #
    # Check A — Minimum duration (5 seconds):
    #   Only drops near-zero API artifacts. 5s chosen (not 30s) because:
    #   - During a 5-min major, a PP goal is scored but PP CONTINUES
    #   - NHL API briefly logs a different sit code at the goal moment
    #   - This creates valid sub-windows of 10-20s that must NOT be dropped
    #   - Only genuine zero-duration artifacts (< 5s) should be discarded
    #
    # Check B — Penalty validation (360 second lookback):
    #   Every PP window must have a penalty logged within 360s before it starts
    #   OR within 60s after it starts (for period carry-overs).
    #   360s = 300s (max 5-min major) + 60s (faceoff delay buffer).
    #   This correctly validates all PP types:
    #     - 2-min minors: penalty within 120s before PP faceoff
    #     - 5-min majors: penalty within 360s before any sub-window
    #     - Carry-over PPs: penalty found within 60s after period start
    #   EN windows are NOT validated — pulling goalie requires no penalty.

    MIN_PP_DURATION    = 5    # seconds — only drop near-zero API artifacts
    MAX_PENALTY_LOOKBACK = 360  # seconds — covers 5-min majors + faceoff delay

    penalty_elapsed_times = sorted([
        p["elapsed"] for p in sorted_plays
        if p.get("type_key", "") in ("penalty", "delayed-penalty")
    ])

    def has_valid_penalty(win_start: int) -> bool:
        lookback_start = win_start - MAX_PENALTY_LOOKBACK
        lookahead_end  = win_start + 60
        return any(lookback_start <= t <= lookahead_end for t in penalty_elapsed_times)

    validated = []
    for w in merged:
        w_start, w_end, w_sit = w
        duration = w_end - w_start if w_end < 99999 else 9999
        is_pp    = "PP" in w_sit

        if is_pp:
            # Check A: drop near-zero artifacts only
            if duration < MIN_PP_DURATION:
                # Strip PP label, keep EN if present, default to 5v5
                base = w_sit.replace(" PP", "").strip()
                validated.append([w_start, w_end, base if base else "5v5"])
                continue
            # Check B: must have a causative penalty
            if not has_valid_penalty(w_start):
                base = w_sit.replace(" PP", "").strip()
                validated.append([w_start, w_end, base if base else "5v5"])
                continue

        validated.append(list(w))

    # Final merge — downgraded PP windows may now be adjacent 5v5 windows
    final = []
    for w in validated:
        if final and final[-1][2] == w[2]:
            final[-1][1] = w[1]
        else:
            final.append(w)

    return [(w[0], w[1], w[2]) for w in final if w[1] > w[0]]


def find_nhl_situation(espn_play: dict, nhl_plays: list, windows: list) -> str:
    """
    Find on-ice situation for an ESPN play using situation windows.
    Primary: window lookup [start, end).
    Fallback: nearest window within FUZZY_SECONDS for gaps.
    """
    if not windows:
        return ""

    elapsed = espn_play.get("elapsed", 0)

    for (w_start, w_end, w_sit) in windows:
        if w_start <= elapsed < w_end:
            return w_sit

    # Final window (closed end)
    if windows and elapsed >= windows[-1][0]:
        return windows[-1][2]

    # Nearest window fallback for micro-gaps
    best     = None
    best_gap = FUZZY_SECONDS + 1
    for (w_start, w_end, w_sit) in windows:
        gap = min(abs(elapsed - w_start), abs(elapsed - w_end))
        if gap < best_gap:
            best_gap = gap
            best     = w_sit
    return best or ""

# =========================
# ESPN SCOREBOARD
# =========================
@st.cache_data(ttl=30, show_spinner=False)
def fetch_scoreboard(date_str: str) -> list:
    date_compact = date_str.replace("-", "")
    try:
        resp = requests.get(
            ESPN_SCOREBOARD,
            params={"dates": date_compact, "limit": 25},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        st.error(f"ESPN error: {e}")
        return []

    games = []
    for event in data.get("events", []):
        comp       = event.get("competitions", [{}])[0]
        status     = comp.get("status", {})
        state_type = status.get("type", {})
        state      = state_type.get("state", "pre")
        raw_name   = state_type.get("name", "")
        display_status = "Scheduled" if raw_name == "STATUS_SCHEDULED" else state_type.get("shortDetail", "")

        competitors = comp.get("competitors", [])
        try:
            away = next(c for c in competitors if c.get("homeAway") == "away")
            home = next(c for c in competitors if c.get("homeAway") == "home")
        except StopIteration:
            continue

        start_dt = to_et(event.get("date", ""))
        is_final = state == "post"
        is_live  = state == "in"

        games.append({
            "event_id":    event.get("id", ""),
            "state":       state,
            "state_name":  display_status,
            "is_live":     is_live,
            "is_final":    is_final,
            "is_ot":       status.get("period", 0) > 3,
            "away_abbr":   away.get("team", {}).get("abbreviation", "?"),
            "home_abbr":   home.get("team", {}).get("abbreviation", "?"),
            "away_logo":   away.get("team", {}).get("logo", ""),
            "home_logo":   home.get("team", {}).get("logo", ""),
            "away_score":  int(away.get("score", 0) or 0),
            "home_score":  int(home.get("score", 0) or 0),
            "has_score":   is_live or is_final,
            "time_str":    fmt_game_time(start_dt),
            "date_str":    date_str,
        })
    return sorted(games, key=lambda x: x["time_str"])

# =========================
# ESPN + NHL HYBRID PLAYS
# =========================
def get_parsed_plays(event_id: str, nhl_game_id: str) -> list:
    st.session_state.last_refresh = datetime.now(ET)

    if st.session_state.cached_event_id == event_id and st.session_state.cached_plays:
        return st.session_state.cached_plays

    try:
        resp = requests.get(ESPN_SUMMARY, params={"event": event_id}, timeout=15)
        resp.raise_for_status()
        raw_plays = resp.json().get("plays", [])
    except Exception as e:
        st.error(f"ESPN error: {e}")
        return []

    nhl_plays = fetch_nhl_plays(nhl_game_id) if nhl_game_id else []
    windows   = build_situation_windows(nhl_plays)

    plays = []
    for p in raw_plays:
        period_obj = p.get("period", {})
        pnum       = period_obj.get("number", 1) if isinstance(period_obj, dict) else 1
        ptype      = period_obj.get("type", "")  if isinstance(period_obj, dict) else ""
        clock_obj  = p.get("clock", {})
        clock_val  = clock_obj.get("displayValue", "") if isinstance(clock_obj, dict) else str(clock_obj)
        type_obj   = p.get("type", {})
        type_text  = type_obj.get("text", "") if isinstance(type_obj, dict) else str(type_obj)
        text       = p.get("text", "")
        wall_raw   = p.get("wallclock", "")
        seq        = int(p.get("sequenceNumber", 0))
        elapsed    = espn_clock_to_seconds(clock_val, pnum)

        situation = find_nhl_situation(
            {"period_num": pnum, "elapsed": elapsed, "type_text": type_text},
            nhl_plays,
            windows,
        )

        plays.append({
            "seq":          seq,
            "period_num":   pnum,
            "period_type":  ptype,
            "period_label": period_label(pnum, ptype),
            "clock":        clock_val,
            "elapsed":      elapsed,
            "type_text":    type_text,
            "text":         text,
            "situation":    situation,
            "wall_raw":     wall_raw,
            "wall_et":      fmt_et_full(wall_raw),
            "wall_dt":      to_et(wall_raw),
            "away_score":   p.get("awayScore", ""),
            "home_score":   p.get("homeScore", ""),
            "emoji":        get_play_emoji(type_text),
        })

    plays.sort(key=lambda x: x["seq"])
    st.session_state.cached_plays    = plays
    st.session_state.cached_event_id = event_id
    return plays

# =========================
# SHARED CSS
# =========================
st.markdown("""
<style>
div[data-testid="stVerticalBlockBorderWrapper"] { min-height: 150px; }
.sched-team-row { display: flex; align-items: center; gap: 12px; margin-bottom: 6px; }
.sched-team-name { font-size: 22px; font-weight: 800; color: #ffffff; }
.sched-score { font-size: 22px; font-weight: 800; color: #888888; margin-left: auto; }
.sched-meta { font-size: 13px; color: #999999; border-top: 1px solid rgba(255,255,255,0.1);
    padding-top: 8px; margin-top: 8px; display: flex; align-items: center; }
.sched-extra { background: #e67e22; color: #fff; font-size: 11px; padding: 2px 6px;
    border-radius: 4px; margin-left: 8px; font-weight: bold; }
</style>
""", unsafe_allow_html=True)

# ======================================================
# GAME FEED VIEW
# ======================================================
if st.session_state.view == "game":

    plays = get_parsed_plays(
        st.session_state.event_id,
        st.session_state.nhl_game_id or "",
    )

    nav_col1, nav_col2, nav_col3, _ = st.columns([1.3, 1, 1.8, 5.9])
    with nav_col1:
        if st.button("⬅ Back to Schedule", use_container_width=True):
            st.session_state.view            = "schedule"
            st.session_state.filters_applied = False
            st.session_state.filtered_plays  = None
            st.rerun()
    with nav_col2:
        if st.button("🔄 Refresh", use_container_width=True):
            st.session_state.cached_plays    = None
            st.session_state.cached_event_id = None
            st.rerun()
    with nav_col3:
        refresh_time = st.session_state.last_refresh.strftime("%H:%M:%S ET")
        st.markdown(
            f'<div style="background-color:#2e7d32;color:white;padding:8px 16px;'
            f'border-radius:4px;font-size:14px;font-weight:bold;text-align:center;">'
            f'Last refresh {refresh_time}</div>',
            unsafe_allow_html=True,
        )

    st.markdown("<br>", unsafe_allow_html=True)
    head_c1, head_c2, head_c3 = st.columns([1, 6, 1])
    with head_c1:
        if st.session_state.away_logo:
            st.image(st.session_state.away_logo, width=80)
    with head_c2:
        st.markdown(f"""
            <div style="display:flex;align-items:center;justify-content:center;
                font-weight:800;font-size:clamp(20px,3vw,32px);gap:15px;text-align:center;">
                <span>{st.session_state.away}</span>
                <span style="color:#888;">{st.session_state.away_score}</span>
                <span style="color:#444;">-</span>
                <span style="color:#888;">{st.session_state.home_score}</span>
                <span>{st.session_state.home}</span>
            </div>
        """, unsafe_allow_html=True)
    with head_c3:
        if st.session_state.home_logo:
            st.image(st.session_state.home_logo, width=80)

    nhl_id = st.session_state.nhl_game_id
    st.caption(
        f"📡 NHL Game ID `{nhl_id}`" if nhl_id
        else "📡 NHL Game ID Not Found — PP & EN unavailable"
    )
    st.divider()

    # ── Filters ───────────────────────────────────────────────────────────
    raw_periods = list({p["period_label"] for p in plays})
    def p_key(l):
        if l.startswith("P"): return int(l[1:])
        if l == "OT": return 100
        if l.startswith("OT"): return 100 + int(l[2:])
        return 200
    all_periods = sorted(raw_periods, key=p_key)

    all_dts            = [p["wall_dt"] for p in plays if p["wall_dt"]]
    game_start_default = min(all_dts) if all_dts else None
    game_end_default   = max(all_dts) if all_dts else None

    USE_PERIOD_FILTER = st.checkbox("🏒 Filter by Period", value=False, key="cb_period")
    selected_periods  = st.multiselect("Select Periods", options=all_periods) if USE_PERIOD_FILTER else []

    USE_TIME_FILTER = st.checkbox("🕐 Filter by Actual Time (ET)", value=False, key="cb_time")
    START_DT = END_DT = None
    if USE_TIME_FILTER:
        def_sd = game_start_default.date() if game_start_default else ddate.today()
        def_ed = game_end_default.date()   if game_end_default   else ddate.today()
        def_st = game_start_default.time() if game_start_default else dtime(19, 0)
        def_et = game_end_default.time()   if game_end_default   else dtime(23, 59)
        st.markdown("**Start date/time (ET)**")
        sc1, sc2 = st.columns(2)
        with sc1: start_date_input = st.date_input("Start date", value=def_sd, key="tf_start_date")
        with sc2: start_time_input = st.time_input("Start time", value=def_st, step=60, key="tf_start_time")
        st.markdown("**End date/time (ET)**")
        ec1, ec2 = st.columns(2)
        with ec1: end_date_input = st.date_input("End date", value=def_ed, key="tf_end_date")
        with ec2: end_time_input = st.time_input("End time", value=def_et, step=60, key="tf_end_time")
        START_DT = datetime.combine(start_date_input, start_time_input).replace(tzinfo=ET)
        END_DT   = datetime.combine(end_date_input,   end_time_input).replace(tzinfo=ET)

    USE_GOAL_FILTER = st.checkbox("🚨 Goals Only",       value=False, key="cb_goals")
    USE_PP_FILTER   = st.checkbox("⚡ Power Plays Only", value=False, key="cb_pp")
    USE_GP_FILTER   = st.checkbox("🥅 Empty Nets Only",  value=False, key="cb_en")

    btn_col1, btn_col2, _ = st.columns([1.5, 1.5, 7])

    with btn_col1:
        if st.button("🚀 Apply Filters", use_container_width=True):
            def passes(p):
                sit = p.get("situation", "")
                if USE_PERIOD_FILTER and selected_periods and p["period_label"] not in selected_periods:
                    return False
                if USE_TIME_FILTER:
                    if not p["wall_dt"] or START_DT is None or END_DT is None: return False
                    if not (START_DT <= p["wall_dt"] <= END_DT): return False
                if USE_GOAL_FILTER and p["type_text"] != "Goal": return False
                if USE_PP_FILTER and "PP" not in sit: return False
                if USE_GP_FILTER and "EN" not in sit: return False
                return True
            st.session_state.filtered_plays  = [p for p in plays if passes(p)]
            st.session_state.filters_applied = True
            st.rerun()

    with btn_col2:
        def reset_filters():
            st.session_state.filters_applied = False
            st.session_state.filtered_plays  = None
            st.session_state.cb_period = False
            st.session_state.cb_time   = False
            st.session_state.cb_goals  = False
            st.session_state.cb_pp     = False
            st.session_state.cb_en     = False
        st.button("🗑️ Remove Filters", use_container_width=True, on_click=reset_filters)

    # ── Display ───────────────────────────────────────────────────────────
    filters_applied = st.session_state.get("filters_applied")
    display_list    = st.session_state.filtered_plays if filters_applied else plays
    total   = len(plays)
    showing = len(display_list)

    if filters_applied:
        if showing == 0:
            st.warning("⚠️ No results found — please check the filters applied.")
            st.stop()
        if USE_PERIOD_FILTER:
            labels = selected_periods if selected_periods else ["none selected"]
            st.info(f"🏒 **Period filter:** {', '.join(labels)} — showing **{showing}** of **{total}** plays")
        if USE_TIME_FILTER:
            st.info(f"🕐 **Time filter:** {START_DT.strftime('%Y-%m-%d %H:%M')} → {END_DT.strftime('%Y-%m-%d %H:%M')} ET — showing **{showing}** of **{total}** plays")
        if USE_GOAL_FILTER:
            n_goals = sum(1 for p in plays if p["type_text"] == "Goal")
            st.info(f"🚨 **Goals Only:** {n_goals} goal(s) in game — showing **{showing}** of **{total}** plays")
        if USE_PP_FILTER:
            st.info(f"⚡ **Power Plays Only:** showing **{showing}** of **{total}** plays")
        if USE_GP_FILTER:
            st.info(f"🥅 **Empty Nets Only:** showing **{showing}** of **{total}** plays")

    for p in display_list:
        emoji = "🚨" if p.get("type_text") == "Goal" else p.get("emoji", "🏒")
        st.subheader(f"{emoji} {p.get('period_label')} | ⏱️ {p.get('clock')}")
        st.markdown(f"📊 **Score:** {p.get('away_score')} - {p.get('home_score')}")
        st.markdown(f"🎯 **Event:** {p.get('type_text')}")
        sit = p.get("situation", "")
        if sit:
            st.markdown(f"⚖️ **Strength:** `{sit}`")
        st.markdown(f"📋 **Play:** {p.get('text')}")
        if p.get("wall_et") and p.get("wall_et") != "N/A":
            st.markdown(f"🕐 **Time (ET):** `{p['wall_et']}`")
        st.divider()

# ======================================================
# SCHEDULE VIEW
# ======================================================
else:
    def handle_date_change():
        st.session_state.sched_date = st.session_state.calendar_widget

    date = st.date_input(
        "Select date",
        value=st.session_state.sched_date,
        key="calendar_widget",
        on_change=handle_date_change,
    )
    formatted_date = date.strftime("%Y-%m-%d")
    games = fetch_scoreboard(formatted_date)

    if not games:
        st.info(f"No games scheduled for {formatted_date}.")
    else:
        cols = st.columns(2)
        for i, g in enumerate(games):
            has_started = g["has_score"]
            ot_badge    = '<span class="sched-extra">OT</span>' if g["is_ot"] else ""
            card_html   = f"""
            <div class="sched-team-row">
                <img src="{g['away_logo']}" width="34"/>
                <span class="sched-team-name">{g['away_abbr']}</span>
                <span class="sched-score">{g['away_score'] if has_started else ''}</span>
            </div>
            <div class="sched-team-row">
                <img src="{g['home_logo']}" width="34"/>
                <span class="sched-team-name">{g['home_abbr']}</span>
                <span class="sched-score">{g['home_score'] if has_started else ''}</span>
            </div>
            <div class="sched-meta">{g['time_str']} &middot; {g['state_name']}{ot_badge}</div>
            """
            with cols[i % 2]:
                with st.container(border=True):
                    st.markdown(card_html, unsafe_allow_html=True)
                    btn_label = (
                        f"▶ Open {g['away_abbr']} @ {g['home_abbr']}"
                        if has_started else "⏳ Not Started"
                    )
                    if st.button(
                        btn_label,
                        key=f"btn_{g['event_id']}",
                        use_container_width=True,
                        disabled=not has_started,
                        help="" if has_started else "Data available once game starts.",
                    ):
                        nhl_id = find_nhl_game_id(
                            formatted_date,
                            g["away_abbr"],
                            g["home_abbr"],
                        )
                        st.session_state.update({
                            "view":            "game",
                            "event_id":        g["event_id"],
                            "nhl_game_id":     nhl_id,
                            "away":            g["away_abbr"],
                            "home":            g["home_abbr"],
                            "away_logo":       g["away_logo"],
                            "home_logo":       g["home_logo"],
                            "away_score":      g["away_score"],
                            "home_score":      g["home_score"],
                            "game_state":      g["state"],
                            "filters_applied": False,
                            "filtered_plays":  None,
                            "cached_plays":    None,
                            "cached_event_id": None,
                        })
                        st.rerun()
