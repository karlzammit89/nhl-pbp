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
def to_et(raw):
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(ET)
    except Exception:
        return None

def fmt_et_full(raw):
    dt = to_et(raw)
    if not dt:
        return "N/A"
    label = "EDT" if dt.dst() != timedelta(0) else "EST"
    return dt.strftime(f"%Y-%m-%d %H:%M:%S {label}")

def fmt_game_time(dt):
    return dt.strftime("%H:%M ET") if dt else "TBD"

def get_play_emoji(type_str):
    t = (type_str or "").lower()
    for k, v in PLAY_EMOJI.items():
        if k in t:
            return v
    return "🏒"

def period_label(period_num, period_type=""):
    pt = (period_type or "").lower()
    if "overtime" in pt or (isinstance(period_num, int) and period_num > 3):
        ot_num = period_num - 3
        return f"OT{ot_num}" if ot_num > 1 else "OT"
    if "shootout" in pt:
        return "SO"
    return f"P{period_num}"

def espn_clock_to_seconds(clock_str, period_num):
    """ESPN clock counts UP from 0:00 (elapsed)."""
    try:
        mm, ss = clock_str.strip().split(":")
        elapsed = int(mm) * 60 + int(ss)
    except Exception:
        elapsed = 0
    return (period_num - 1) * 1200 + elapsed

def nhl_clock_to_seconds(time_in_period, period_num):
    """NHL clock counts DOWN from 20:00 (remaining)."""
    try:
        mm, ss = time_in_period.strip().split(":")
        elapsed = 1200 - (int(mm) * 60 + int(ss))
    except Exception:
        elapsed = 0
    return (period_num - 1) * 1200 + elapsed

def parse_nhl_situation(sit_code):
    """
    Parse NHL situation code: [away_goalie][away_sk][home_sk][home_goalie]
    Returns e.g. '5v5', '4v5 PP', '6v5 Away EN PP'
    """
    if not sit_code or len(sit_code) < 4:
        return ""
    try:
        a = int(sit_code[1])
        h = int(sit_code[2])
    except ValueError:
        return ""
    away_en = sit_code[0] == "0"
    home_en = sit_code[3] == "0"
    is_pp   = a != h
    parts   = [f"{a}v{h}"]
    if away_en and home_en:
        parts.append("Both EN")
    elif away_en:
        parts.append("Away EN")
    elif home_en:
        parts.append("Home EN")
    if is_pp:
        parts.append("PP")
    return " ".join(parts)

# =========================
# NHL GAME ID LOOKUP
# =========================
@st.cache_data(ttl=3600, show_spinner=False)
def find_nhl_game_id(date_str, away_abbr, home_abbr):
    try:
        data = requests.get(f"{NHL_SCHEDULE}/{date_str}", timeout=10).json()
    except Exception:
        return ""
    target = datetime.fromisoformat(date_str).date()
    for day in data.get("gameWeek", []):
        for g in day.get("games", []):
            dt = to_et(g.get("startTimeUTC", ""))
            if dt and dt.date() != target:
                continue
            nhl_away = g.get("awayTeam", {}).get("abbrev", "").upper()
            nhl_home = g.get("homeTeam", {}).get("abbrev", "").upper()
            if _abbr_match(away_abbr, nhl_away) and _abbr_match(home_abbr, nhl_home):
                return str(g.get("id", ""))
    return ""

def _abbr_match(a, b):
    a, b = a.upper(), b.upper()
    if a == b:
        return True
    KNOWN = {
        "VGK": ["VEG","VGK","LV"], "VEG": ["VGK","VEG","LV"], "LV": ["VGK","VEG"],
        "UTA": ["UTAH","UTA"], "WSH": ["WAS","WSH"], "WAS": ["WSH","WAS"],
        "CLB": ["CBJ","CLB"], "CBJ": ["CLB","CBJ"],
    }
    return a in KNOWN.get(b, []) or b in KNOWN.get(a, [])

# =========================
# NHL PLAY-BY-PLAY FETCH
# =========================
# Penalty types that do NOT create a power play:
NO_PP_PENALTY_TYPES = {
    "misconduct",       # 10-min, player replaced
    "game-misconduct",  # rest of game, player replaced
    "fighting",         # offsetting, 5v5 continues unless one side has more
}

@st.cache_data(ttl=30, show_spinner=False)
def fetch_nhl_plays(nhl_game_id):
    """
    Fetch NHL play-by-play and parse plays + penalty details.
    Returns dict with:
      - plays: list of plays with sit_code/situation
      - penalties: list of penalty details (duration, type, team)
      - teams: {'away_id': ..., 'home_id': ...}
    """
    if not nhl_game_id:
        return {"plays": [], "penalties": [], "teams": {}}
    try:
        resp = requests.get(f"{NHL_PBP}/{nhl_game_id}/play-by-play", timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return {"plays": [], "penalties": [], "teams": {}}

    teams = {
        "away_id": data.get("awayTeam", {}).get("id"),
        "home_id": data.get("homeTeam", {}).get("id"),
        "away_abbr": (data.get("awayTeam", {}).get("abbrev") or "").upper(),
        "home_abbr": (data.get("homeTeam", {}).get("abbrev") or "").upper(),
    }

    plays = []
    penalties = []

    for p in data.get("plays", []):
        pd       = p.get("periodDescriptor", {})
        pnum     = pd.get("number", 1)
        ptype    = pd.get("periodType", "REG")
        tip      = p.get("timeInPeriod", "20:00")
        sit      = p.get("situationCode") or ""
        type_key = p.get("typeDescKey", "")
        details  = p.get("details") or {}

        elapsed = nhl_clock_to_seconds(tip, pnum)

        plays.append({
            "period":     pnum,
            "period_type":ptype,
            "elapsed":    elapsed,
            "time_in":    tip,
            "type_key":   type_key,
            "sit_code":   sit,
            "situation":  parse_nhl_situation(sit),
            "sort_order": p.get("sortOrder", 0),
        })

        # Extract penalty details — NHL API gives us the duration directly
        if type_key in ("penalty", "delayed-penalty"):
            desc_key       = (details.get("descKey") or "").lower()
            duration_min   = details.get("duration")   # already in minutes
            committed_id   = details.get("committedByPlayerId")
            event_team_id  = details.get("eventOwnerTeamId")  # team that took penalty

            # Determine penalty side
            pen_side = ""
            if event_team_id == teams["away_id"]:
                pen_side = "away"
            elif event_team_id == teams["home_id"]:
                pen_side = "home"

            penalties.append({
                "elapsed":      elapsed,
                "period":       pnum,
                "period_type":  ptype,
                "desc_key":     desc_key,                  # e.g. "high-sticking"
                "duration_min": duration_min,              # 2, 4, 5, 10
                "duration_sec": (duration_min or 2) * 60,
                "pen_side":     pen_side,                  # "away" or "home"
                "is_no_pp":     desc_key in NO_PP_PENALTY_TYPES,
                "type_key":     type_key,
                "sort_order":   p.get("sortOrder", 0),
            })

    return {"plays": plays, "penalties": penalties, "teams": teams}

# =========================
# COINCIDENTAL PENALTY DETECTION
# =========================
def detect_coincidental_penalties(penalties):
    """
    NHL rule: when both teams take penalties at the same stoppage with
    matching minor durations, they are 'coincidental' — no PP, just 4v4.

    Mark coincidental pairs as is_no_pp=True so they're excluded from
    PP window generation. Penalties within 2 seconds of each other on
    opposing teams with the same duration are considered coincidental.
    """
    for i, p in enumerate(penalties):
        if p["is_no_pp"]:
            continue
        for j, q in enumerate(penalties):
            if i == j or q["is_no_pp"]:
                continue
            if (p["pen_side"] != q["pen_side"]
                and p["pen_side"] and q["pen_side"]
                and abs(p["elapsed"] - q["elapsed"]) <= 2
                and p["duration_min"] == q["duration_min"]
                and p["duration_min"] in (2, 4)):  # minors / double minors coincide
                # Mark both as coincidental
                p["is_no_pp"]   = True
                p["coincident"] = True
                q["is_no_pp"]   = True
                q["coincident"] = True
    return penalties

# =========================
# ESPN PENALTY PARSER (FALLBACK)
# =========================
ESPN_PENALTY_DURATIONS = [
    ("double minor",    240),
    ("game misconduct", None),
    ("misconduct",      None),
    ("major",           300),
    ("match",           300),
    ("bench minor",     120),
    ("minor",           120),
]
ESPN_NO_PP_KEYWORDS = [
    "coincidental", "offsetting", "matching penalties",
    "double minor to each", "roughing to each",
]

def parse_espn_penalties(espn_plays, away_abbr, home_abbr):
    """
    Fallback penalty parser using ESPN play text.
    Only used when NHL API data is unavailable or incomplete.
    """
    penalties = []
    for p in espn_plays:
        type_obj  = p.get("type", {})
        type_text = (type_obj.get("text", "") if isinstance(type_obj, dict) else "").lower()
        if "penalty" not in type_text:
            continue

        text = (p.get("text") or "").lower()

        # Skip no-PP situations
        if any(kw in text for kw in ESPN_NO_PP_KEYWORDS):
            continue

        # Determine duration (longest match first)
        duration = None
        is_dm = is_major = False
        for key, dur in ESPN_PENALTY_DURATIONS:
            if key in text:
                duration = dur
                is_dm    = (key == "double minor")
                is_major = key in ("major", "match")
                break
        if duration is None:
            duration = 120
        if duration == 0 or duration is None:
            continue

        period_obj = p.get("period", {})
        pnum       = period_obj.get("number", 1) if isinstance(period_obj, dict) else 1
        clock_obj  = p.get("clock", {})
        clock_val  = clock_obj.get("displayValue", "0:00") if isinstance(clock_obj, dict) else "0:00"
        elapsed    = espn_clock_to_seconds(clock_val, pnum)

        team_obj = p.get("team", {})
        pen_team_abbr = team_obj.get("abbreviation", "").upper() if isinstance(team_obj, dict) else ""

        if pen_team_abbr == away_abbr.upper():
            sit_label = "5v4 PP"
            pen_side  = "away"
        elif pen_team_abbr == home_abbr.upper():
            sit_label = "4v5 PP"
            pen_side  = "home"
        else:
            sit_label = "PP"
            pen_side  = ""

        penalties.append({
            "elapsed":      elapsed,
            "period":       pnum,
            "duration_sec": duration,
            "sit_label":    sit_label,
            "is_dm":        is_dm,
            "is_major":     is_major,
            "pen_side":     pen_side,
        })
    return penalties

def build_espn_pp_windows(espn_penalties, espn_plays):
    """
    Build PP windows from ESPN penalty data using NHL rules.
    Minor: ends at +120s OR first PP goal.
    Double minor: first goal ends first 2-min half; second half always runs full.
    Major: full 5 min regardless of goals.
    """
    if not espn_penalties:
        return []

    pp_goal_times = []
    for p in espn_plays:
        text      = (p.get("text") or "").lower()
        type_obj  = p.get("type", {})
        type_text = (type_obj.get("text", "") if isinstance(type_obj, dict) else "").lower()
        if "goal" in type_text and "power play" in text:
            period_obj = p.get("period", {})
            pnum       = period_obj.get("number", 1) if isinstance(period_obj, dict) else 1
            clock_obj  = p.get("clock", {})
            clock_val  = clock_obj.get("displayValue", "0:00") if isinstance(clock_obj, dict) else "0:00"
            pp_goal_times.append(espn_clock_to_seconds(clock_val, pnum))
    pp_goal_times.sort()

    windows = []
    for pen in sorted(espn_penalties, key=lambda x: x["elapsed"]):
        start = pen["elapsed"]
        dur   = pen["duration_sec"]
        sit   = pen["sit_label"]
        if pen["is_dm"]:
            mid = start + 120
            end = start + 240
            first_goal = next((t for t in pp_goal_times if start < t <= mid), None)
            if first_goal:
                windows.append((start, first_goal, sit))
                windows.append((mid, end, sit))
            else:
                windows.append((start, end, sit))
        elif pen["is_major"]:
            windows.append((start, start + dur, sit))
        else:
            end = start + dur
            first_goal = next((t for t in pp_goal_times if start < t <= end), None)
            windows.append((start, first_goal if first_goal else end, sit))

    return windows

# =========================
# SITUATION WINDOW BUILDER
# =========================
def build_situation_windows(nhl_data, espn_plays=None, away_abbr="", home_abbr=""):
    """
    Build gapless situation windows using a hybrid approach.

    PRIMARY: NHL API situationCode (most accurate)
      Phase 1: Build raw windows from every NHL play
      Phase 2: Patch period-start carry-over windows
      Phase 2b: Gap-fill PP windows from NHL penalty data when sit codes missing
      Phase 3: Merge adjacent same-situation windows
      Phase 4: Validate PP windows (penalty required, min duration)

    FALLBACK: ESPN penalty text
      Phase 5: For penalties the NHL API missed entirely, use ESPN penalty
               windows to fill gaps. NHL always wins where both have data.
    """
    nhl_plays      = nhl_data.get("plays", [])
    nhl_penalties  = nhl_data.get("penalties", [])

    # Mark coincidental penalties as no-PP
    nhl_penalties = detect_coincidental_penalties(list(nhl_penalties))

    if not nhl_plays:
        # No NHL data at all — fall back to pure ESPN
        if espn_plays:
            esp_pens = parse_espn_penalties(espn_plays, away_abbr, home_abbr)
            return build_espn_pp_windows(esp_pens, espn_plays)
        return []

    BOUNDARY_TYPES = {
        "period-start", "period-end", "game-start", "game-end",
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

    # ── Phase 2b: gap-fill PP windows from penalty data ────────────────────
    # For every penalty that gave a real PP, check if a PP window covers it.
    # If not, look for ANY play with a PP sit code after the penalty and
    # use it to synthesise the window.
    for pen in nhl_penalties:
        if pen["is_no_pp"]:
            continue
        pen_e   = pen["elapsed"]
        expect_end = pen_e + pen["duration_sec"] + 60   # PP duration + buffer

        covered = any(
            "PP" in w[3] and w[0] <= expect_end and w[1] >= pen_e
            for w in raw_windows
        )
        if covered:
            continue

        first_pp = next(
            (p for p in sorted_plays
             if p["elapsed"] > pen_e
             and p["elapsed"] <= expect_end
             and "PP" in p["situation"]),
            None
        )
        if not first_pp:
            continue

        synth_end = next(
            (p["elapsed"] for p in sorted_plays
             if p["elapsed"] > first_pp["elapsed"] and "PP" not in p["situation"]),
            first_pp["elapsed"] + pen["duration_sec"]
        )

        synth = [first_pp["elapsed"], synth_end,
                 first_pp["sit_code"], first_pp["situation"], False]
        for i, w in enumerate(raw_windows):
            if w[0] >= synth[0]:
                raw_windows.insert(i, synth)
                break
        else:
            raw_windows.append(synth)

    # ── Phase 3: merge adjacent same-situation windows ────────────────────
    merged = []
    for w in raw_windows:
        sit_str = w[3]
        if merged and merged[-1][2] == sit_str:
            merged[-1][1] = w[1]
        else:
            merged.append([w[0], w[1], sit_str])
    merged = [w for w in merged if w[1] > w[0]]

    # ── Phase 4: validate PP windows against NHL rules ─────────────────────
    # Check A: minimum duration 5s (drop near-zero artifacts only)
    # Check B: must have a non-coincidental penalty within lookback range
    MIN_PP_DURATION    = 5
    MAX_PENALTY_LOOKBACK = 360  # 5-min major + buffer

    # Use only non-coincidental penalties for validation
    valid_penalty_times = sorted(
        p["elapsed"] for p in nhl_penalties if not p["is_no_pp"]
    )

    def has_valid_penalty(win_start):
        lo = win_start - MAX_PENALTY_LOOKBACK
        hi = win_start + 60
        return any(lo <= t <= hi for t in valid_penalty_times)

    validated = []
    for w in merged:
        w_start, w_end, w_sit = w
        dur   = w_end - w_start if w_end < 99999 else 9999
        is_pp = "PP" in w_sit
        if is_pp:
            if dur < MIN_PP_DURATION:
                base = w_sit.replace(" PP", "").strip()
                validated.append([w_start, w_end, base or "5v5"])
                continue
            if not has_valid_penalty(w_start):
                base = w_sit.replace(" PP", "").strip()
                validated.append([w_start, w_end, base or "5v5"])
                continue
        validated.append(list(w))

    # Merge after validation
    final_nhl = []
    for w in validated:
        if final_nhl and final_nhl[-1][2] == w[2]:
            final_nhl[-1][1] = w[1]
        else:
            final_nhl.append(w)
    final_nhl = [(w[0], w[1], w[2]) for w in final_nhl if w[1] > w[0]]

    # ── Phase 5: ESPN fallback for PPs the NHL API missed entirely ─────────
    if espn_plays and (away_abbr or home_abbr):
        esp_pens  = parse_espn_penalties(espn_plays, away_abbr, home_abbr)
        espn_wins = build_espn_pp_windows(esp_pens, espn_plays)

        for (es, ee, esit) in espn_wins:
            nhl_covers = any(
                "PP" in nhl_sit and ns <= es + 30 and ne >= ee - 30
                for (ns, ne, nhl_sit) in final_nhl
            )
            if nhl_covers:
                continue
            insert_pos = len(final_nhl)
            for i, (ns, ne, _) in enumerate(final_nhl):
                if ns >= es:
                    insert_pos = i
                    break
            final_nhl.insert(insert_pos, (es, ee, esit))

        final_nhl.sort(key=lambda w: w[0])

    return final_nhl

def find_nhl_situation(espn_play, windows):
    """Find on-ice situation for an ESPN play from situation windows."""
    if not windows:
        return ""
    elapsed = espn_play.get("elapsed", 0)
    for (ws, we, wsit) in windows:
        if ws <= elapsed < we:
            return wsit
    if windows and elapsed >= windows[-1][0]:
        return windows[-1][2]
    best, best_gap = None, FUZZY_SECONDS + 1
    for (ws, we, wsit) in windows:
        gap = min(abs(elapsed - ws), abs(elapsed - we))
        if gap < best_gap:
            best_gap, best = gap, wsit
    return best or ""

# =========================
# ESPN SCOREBOARD
# =========================
@st.cache_data(ttl=30, show_spinner=False)
def fetch_scoreboard(date_str):
    try:
        data = requests.get(
            ESPN_SCOREBOARD,
            params={"dates": date_str.replace("-", ""), "limit": 25},
            timeout=10,
        ).json()
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
# HYBRID PLAY PARSER
# =========================
def get_parsed_plays(event_id, nhl_game_id, away_abbr="", home_abbr=""):
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

    nhl_data = fetch_nhl_plays(nhl_game_id) if nhl_game_id else {"plays": [], "penalties": [], "teams": {}}
    windows  = build_situation_windows(nhl_data, espn_plays=raw_plays,
                                        away_abbr=away_abbr, home_abbr=home_abbr)

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

        situation = find_nhl_situation({"elapsed": elapsed}, windows)

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
# CSS
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
        st.session_state.away,
        st.session_state.home,
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
        f"📡 NHL `{nhl_id}` + ESPN hybrid" if nhl_id
        else "📡 ESPN only — NHL ID not found"
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

    all_dts  = [p["wall_dt"] for p in plays if p["wall_dt"]]
    g_start  = min(all_dts) if all_dts else None
    g_end    = max(all_dts) if all_dts else None

    USE_PERIOD_FILTER = st.checkbox("🏒 Filter by Period", value=False, key="cb_period")
    selected_periods  = st.multiselect("Select Periods", options=all_periods) if USE_PERIOD_FILTER else []

    USE_TIME_FILTER = st.checkbox("🕐 Filter by Actual Time (ET)", value=False, key="cb_time")
    START_DT = END_DT = None
    if USE_TIME_FILTER:
        def_sd = g_start.date() if g_start else ddate.today()
        def_ed = g_end.date()   if g_end   else ddate.today()
        def_st = g_start.time() if g_start else dtime(19, 0)
        def_et = g_end.time()   if g_end   else dtime(23, 59)
        st.markdown("**Start date/time (ET)**")
        sc1, sc2 = st.columns(2)
        with sc1: start_date_input = st.date_input("Start date", value=def_sd, key="tf_start_date")
        with sc2: start_time_input = st.time_input("Start time", value=def_st, step=60, key="tf_start_time")
        st.markdown("**End date/time (ET)**")
        ec1, ec2 = st.columns(2)
        with ec1: end_date_input = st.date_input("End date", value=def_ed, key="tf_end_date")
        with ec2: end_time_input = st.time_input("End time", value=def_et, step=60, key="tf_et")
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
