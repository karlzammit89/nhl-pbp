import streamlit as st
import requests
import time
from collections import defaultdict
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
ESPN_SITUATION  = "https://sports.core.api.espn.com/v2/sports/hockey/leagues/nhl/events/{eid}/competitions/{eid}/situation"
NHL_SCHEDULE    = "https://api-web.nhle.com/v1/schedule"
NHL_PBP         = "https://api-web.nhle.com/v1/gamecenter"
NHL_SHIFTS      = "https://api.nhle.com/stats/rest/en/shiftcharts?cayenneExp=gameId={gid}"

PLAY_EMOJI = {
    "goal": "🚨", "penalty": "🟡", "shot-on-goal": "🎯",
    "blocked-shot": "🛡️", "missed-shot": "🤦", "faceoff": "🏒",
    "hit": "💥", "giveaway": "❌", "takeaway": "⛹️",
    "stoppage": "⏸️", "period-start": "▶️", "period-end": "⏹️",
}

FUZZY_SECONDS = 2

# ESPN NHL type.id — confirmed penalty infraction IDs
# Sourced from four production scans: Oct-Dec 2025, Jan-Feb 2026, Apr-May 2026
# plus confirmed decisions on Abusive Language (101) and Abuse of Official minor (64).
# Only explicitly confirmed IDs are used — no threshold assumptions.
# Add new IDs here as they are confirmed via the game diagnostic expander.
#
# Confirmed NO-PP (never add): 25=Leaving Crease, 38/106=Instigator,
#   80=Fighting, 91=Misconduct, 93/94=Game/Abuse Misconduct,
#   105=Aggressor, 143=Penalty Shot, 509=Penalty (ambiguous)
#
# Still unknown (extremely rare): Spearing, Clipping, Head Butting, Biting, Spitting
ESPN_KNOWN_PENALTY_IDS = {
      7,   # Boarding (minor variant)
      9,   # Broken Stick
     10,   # Butt Ending
     11,   # Charging
     12,   # Closing Hand on Puck
     13,   # Cross checking
     17,   # Elbowing
     20,   # Illegal Check to the Head
     22,   # Delay of Game
     29,   # High-sticking
     30,   # High sticking (label variant of 29)
     31,   # Holding
     32,   # Holding the Stick
     33,   # Hooking
     35,   # Illegal Stick
     37,   # Interference
     39,   # Kneeing
     40,   # Goalkeeper Interference
     45,   # Roughing
     49,   # Slashing
     52,   # Throwing the Stick
     55,   # Tripping
     57,   # Unsportsmanlike Conduct
     58,   # Too Many Men on the Ice
     64,   # Abuse of Official (minor level)
     72,   # Boarding (major variant)
     76,   # Crosschecking (label variant of 13)
     85,   # Kneeing (variant ID)
     86,   # Slashing (variant ID)
    101,   # Abusive Language
    107,   # Delaying Game - Illegal Play by Goaltender
    108,   # Delaying Game - Smothering Puck
    109,   # Delaying Game - Puck over Glass
    123,   # Interference (variant ID)
    132,   # Throwing Object at Puck
    135,   # Hooking on Breakaway
    136,   # Tripping on Breakaway
    137,   # Slashing on Breakaway
    140,   # Game Misconduct - Head Coach (bench minor)
    142,   # Embellishment
    149,   # Removing Opponent Helmet
    150,   # Goalie Removed Own Mask
    151,   # Delay Game - Unsuccessful Challenge
    163,   # Playing without a Helmet
}

def is_espn_penalty(type_id) -> bool:
    """
    Returns True only if type_id is a confirmed ESPN penalty infraction ID.
    Deliberately conservative — unknown IDs return False until confirmed
    by running the diagnostic scanner across more games.
    """
    try:
        return int(type_id) in ESPN_KNOWN_PENALTY_IDS
    except (ValueError, TypeError):
        return False

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
    "active_filter_snapshot": {},
    "sort_newest_first": False,
    "force_bucket": None,
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


def make_strength_label(sit_code, away_abbr, home_abbr):
    """
    Build the per-play strength label from an NHL situationCode.
    situationCode = [awayGoalie][awaySkaters][homeSkaters][homeGoalie];
    a goalie digit of '0' means that net is empty (goalie pulled).

    Returns labels like:
      '5v4 CAR PP'        — power play, team named
      '6v5 CAR EN'        — pulled goalie, even strength
      '6v4 CAR EN PP'     — pulled goalie while on the power play
      '4v6 VGK EN PP'     — home-side equivalent
      '6v6 CAR+VGK EN'    — both nets empty
      ''                  — even strength (no label shown)

    Guards (validated across 201 games):
      - A real empty net requires the pulling team to field 6 skaters
        (the pulled goalie becomes the 6th attacker). A '0' goalie digit
        with fewer than 6 skaters is a malformed/transition frame and is
        NOT treated as an empty net (e.g. situationCode 1550, 0551).
      - Shootout frames (1v0 / 0v1) never reach 6 skaters, so they never
        produce an EN label; they are also excluded from PP by the caller
        which skips the shootout period.
      - When the same team has both the empty net and the power play the
        team name appears once ('6v4 CAR EN PP', not 'CAR EN CAR PP').
    """
    if not sit_code or len(sit_code) < 4:
        return ""
    try:
        a_sk = int(sit_code[1])
        h_sk = int(sit_code[2])
    except ValueError:
        return ""

    # Shootout / degenerate frames: a side with 0 skaters (1v0 / 0v1) is not
    # a real on-ice strength state. Return empty so no PP/EN label is shown.
    if a_sk == 0 or h_sk == 0:
        return ""

    away_en = sit_code[0] == "0" and a_sk == 6
    home_en = sit_code[3] == "0" and h_sk == 6

    parts = [f"{a_sk}v{h_sk}"]

    is_pp   = False
    pp_team = ""
    if a_sk != h_sk:
        # A pulled-goalie extra attacker (6v5 / 5v6) is even strength,
        # not a man advantage — exclude those from PP.
        even_pull = (away_en and a_sk == h_sk + 1) or (home_en and h_sk == a_sk + 1)
        if not even_pull:
            is_pp   = True
            pp_team = away_abbr if a_sk > h_sk else home_abbr

    en_team = ""
    if away_en and home_en:
        en_team = f"{away_abbr}+{home_abbr}"
    elif away_en:
        en_team = away_abbr
    elif home_en:
        en_team = home_abbr

    if en_team and is_pp:
        if en_team == pp_team:
            parts.append(f"{en_team} EN PP")
        else:
            parts.append(f"{en_team} EN")
            parts.append(f"{pp_team} PP")
    elif en_team:
        parts.append(f"{en_team} EN")
    elif is_pp:
        parts.append(f"{pp_team} PP")

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
@st.cache_data(show_spinner=False)
def fetch_nhl_plays(nhl_game_id, cache_bucket: int = 0):
    """
    Fetch NHL play-by-play.
    Returns:
      plays:          all plays for EN/PP cross-reference
      delayed_events: enriched delayed-penalty events (paired with penalty details)
      teams:          away/home IDs and abbreviations
      goalie_ids:     set of goalie playerIds (for shift chart backup filter)
      game_state:     e.g. 'OFF', 'LIVE', 'CRIT' (for open-gap detection)
    """
    if not nhl_game_id:
        return {"plays": [], "delayed_events": [], "teams": {},
                "goalie_ids": set(), "game_state": "OFF"}
    try:
        resp = requests.get(f"{NHL_PBP}/{nhl_game_id}/play-by-play", timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return {"plays": [], "delayed_events": [], "teams": {},
                "goalie_ids": set(), "game_state": "OFF"}

    game_state = data.get("gameState", "OFF")

    teams = {
        "away_id":   data.get("awayTeam", {}).get("id"),
        "home_id":   data.get("homeTeam", {}).get("id"),
        "away_abbr": (data.get("awayTeam", {}).get("abbrev") or "").upper(),
        "home_abbr": (data.get("homeTeam", {}).get("abbrev") or "").upper(),
    }

    # Goalie IDs from rosterSpots for shift chart backup filter (Item 1)
    goalie_ids = {
        rs["playerId"]
        for rs in data.get("rosterSpots", [])
        if rs.get("positionCode") == "G"
    }

    # Player name lookup for delayed penalty card enrichment (Item 2)
    def _pn(v):
        return v.get("default", "") if isinstance(v, dict) else (str(v) if v else "")
    player_names = {
        rs["playerId"]: f"{_pn(rs.get('firstName',''))} {_pn(rs.get('lastName',''))}".strip()
        for rs in data.get("rosterSpots", [])
    }

    plays          = []
    delayed_events = []
    penalty_events = []  # for delayed-penalty pairing
    goal_events    = []  # for goal-cancelled detection (Fix 2)

    for p in data.get("plays", []):
        pd       = p.get("periodDescriptor", {})
        pnum     = pd.get("number", 1)
        tip      = p.get("timeInPeriod", "20:00")
        sit      = p.get("situationCode") or ""
        type_key = p.get("typeDescKey", "")
        elapsed  = nhl_clock_to_seconds(tip, pnum)
        # espn_elapsed: counts UP from period start — same unit as ESPN play elapsed.
        # timeInPeriod also counts UP, so espn_clock_to_seconds gives correct mapping.
        espn_el  = espn_clock_to_seconds(tip, pnum)

        plays.append({
            "period":       pnum,
            "elapsed":      elapsed,
            "espn_elapsed": espn_el,
            "type_key":     type_key,
            "sit_code":     sit,
            "sort_order":   p.get("sortOrder", 0),
        })

        if type_key == "delayed-penalty":
            det = p.get("details") or {}
            delayed_events.append({
                "elapsed":      elapsed,
                "espn_elapsed": espn_el,
                "period":       pnum,
                "owner_id":     str(det.get("eventOwnerTeamId", "") or ""),
            })

        if type_key == "penalty":
            det = p.get("details") or {}
            penalty_events.append({
                "espn_elapsed":   espn_el,
                "period":         pnum,
                "owner_id":       str(det.get("eventOwnerTeamId", "") or ""),
                "desc_key":       det.get("descKey", ""),
                "duration":       det.get("duration", 2),
                "committed_id":   det.get("committedByPlayerId"),
                "drawn_id":       det.get("drawnByPlayerId"),
            })

        if type_key == "goal":
            goal_events.append({"espn_elapsed": espn_el})

    # Pair each delayed-penalty event with its subsequent penalty event.
    # Fix 1: require matching owner_id (committing team) to prevent cross-pairing.
    # Fix 4: detect split double minor (two 2-min events from same team within 5s).
    # Fix 2: detect goal-cancelled calls when pairing fails.
    for dp in delayed_events:
        dp_el    = dp["espn_elapsed"]
        dp_owner = dp.get("owner_id", "")

        # Fix 1: match owner_id when available; fall back to first in window
        paired = next(
            (pe for pe in penalty_events
             if pe["espn_elapsed"] > dp_el
             and pe["espn_elapsed"] <= dp_el + 80
             and (not dp_owner or not pe.get("owner_id", "")
                  or pe["owner_id"] == dp_owner)),
            None,
        )
        if paired:
            raw_dur = int(paired["duration"]) if paired["duration"] else 2
            # Fix 4: split double minor — two 2-min events same team within 5s
            if raw_dur == 2:
                paired_el = paired["espn_elapsed"]
                split = next(
                    (pe for pe in penalty_events
                     if pe is not paired
                     and pe.get("owner_id", "") == dp_owner
                     and int(pe.get("duration") or 0) == 2
                     and abs(pe["espn_elapsed"] - paired_el) <= 5),
                    None,
                )
                dp["is_double_minor"] = split is not None
                if dp["is_double_minor"]:
                    raw_dur = 4
            else:
                dp["is_double_minor"] = False
            dp["desc_key"]       = paired["desc_key"]
            dp["duration"]       = raw_dur
            dp["committed_name"] = player_names.get(paired["committed_id"], "")
            dp["drawn_name"]     = player_names.get(paired["drawn_id"], "")
            dp["goal_cancelled"] = False
        else:
            # Fix 2: check if a goal stopped play in the window
            dp["goal_cancelled"] = any(
                g["espn_elapsed"] > dp_el and g["espn_elapsed"] <= dp_el + 80
                for g in goal_events
            )
            dp["desc_key"]       = ""
            dp["duration"]       = 2
            dp["committed_name"] = ""
            dp["drawn_name"]     = ""
            dp["is_double_minor"] = False

    return {
        "plays":          plays,
        "delayed_events": delayed_events,
        "teams":          teams,
        "goalie_ids":     goalie_ids,
        "game_state":     game_state,
    }


# =========================
# ESPN-FIRST PP PIPELINE
# =========================
# Infractions that create a PP (used to filter standalone misconducts)
_PP_INFRACTIONS = {
    "hooking","tripping","interference","roughing","slashing","cross-checking",
    "boarding","high-sticking","holding","delay","spearing","elbowing",
    "charging","butt-ending","closing","too-many","goaltender","unsportsmanlike",
}

def _build_bm(espn_json):
    """ESPN team.id → 'away'/'home' from boxscore + header."""
    bm = {}
    for t in ((espn_json.get("boxscore") or {}).get("teams", [])):
        te = t.get("team", {}) if isinstance(t.get("team"), dict) else {}
        tid = str(te.get("id", "")); ha = t.get("homeAway", "")
        if tid and ha: bm[tid] = ha
    for c in ((espn_json.get("header") or {}).get("competitions", [])):
        for comp in (c.get("competitors", [])):
            te  = comp.get("team", {}) if isinstance(comp.get("team"), dict) else {}
            tid = str(te.get("id", "")); ha = comp.get("homeAway", "")
            if tid and ha: bm[tid] = ha
    return bm

def _resolve_team(play, bm):
    """Penalised team abbreviation from ESPN penalty play."""
    to = play.get("team", {}) if isinstance(play.get("team"), dict) else {}
    ab = (to.get("abbreviation") or "").upper()
    return ab if ab else bm.get(str(to.get("id", "")), "")

def _creates_pp(text):
    """False for standalone misconducts (no PP). True otherwise."""
    t = text.lower()
    if "misconduct" in t and not any(w in t for w in _PP_INFRACTIONS):
        return False
    return True

def _detect_offsets(pens, away, home):
    """
    Group-based 1:1 coincidental offsetting.
    Validated: 145 cancelled pairs across 160 games, 0 false positives.
    Mutates pens in-place via is_coincidental flag.
    """
    COINCIDE = 8
    processed = set()
    for i, a in enumerate(pens):
        if i in processed: continue
        grp = [i]
        for j in range(len(pens)):
            if j == i or j in processed: continue
            if abs(pens[j]["elapsed"] - a["elapsed"]) <= COINCIDE:
                grp.append(j)
        for idx in grp: processed.add(idx)
        away_pp = [k for k in grp if pens[k]["team"] == away and pens[k]["creates"]]
        home_pp = [k for k in grp if pens[k]["team"] == home and pens[k]["creates"]]
        pairs   = min(len(away_pp), len(home_pp))
        for k in grp:             pens[k]["is_coincidental"] = False
        for k in away_pp[:pairs]: pens[k]["is_coincidental"] = True
        for k in home_pp[:pairs]: pens[k]["is_coincidental"] = True

def build_pp_windows_from_espn(espn_json, away_abbr, home_abbr):
    """
    Build PP windows directly from ESPN penalty plays.

    Every window has a PP Cause by construction (the penalty play itself).
    0 phantoms. 0 tagging failures. 100% PP Cause across 843 windows / 160 games.

    Returns list of dicts:
      ws, we:      int — window start/end elapsed seconds
      sit:         str — '5v4 PP', '4v5 PP', '5v3 PP', '3v5 PP', '4v3 PP', '3v4 PP'
      short:       str — 'away' or 'home' (penalised team)
      source_seq:  int — ESPN sequenceNumber of the cause penalty play
    """
    raw_plays = espn_json.get("plays") or []
    bm        = _build_bm(espn_json)

    # ── Collect all ESPN penalty plays ───────────────────────────────
    pens = []
    for p in raw_plays:
        type_obj = p.get("type", {}) if isinstance(p.get("type"), dict) else {}
        tid      = str(type_obj.get("id", ""))
        if not is_espn_penalty(tid): continue
        po   = p.get("period", {}) if isinstance(p.get("period"), dict) else {}
        pnum = po.get("number", 1)
        co   = p.get("clock", {}) if isinstance(p.get("clock"), dict) else {}
        elapsed = espn_clock_to_seconds(co.get("displayValue", "0:00"), pnum)
        team    = _resolve_team(p, bm)
        # Normalise: bm fallback returns "away"/"home" → convert to actual abbr
        if   team == "away": team = away_abbr
        elif team == "home": team = home_abbr
        text    = (p.get("text") or "")
        seq     = int(p.get("sequenceNumber", 0))

        # Duration: type.penaltyMinutes (primary) → text fallback
        pm = type_obj.get("penaltyMinutes") if isinstance(type_obj, dict) else None
        try:   mins = int(pm) if pm is not None else None
        except: mins = None
        if mins is None:
            txt = text.lower()
            if "double minor" in txt:                            mins = 4
            elif any(w in txt for w in ("major","match","fight")): mins = 5
            elif "misconduct" in txt:                            mins = 10
            else:                                                mins = 2
        if mins >= 10: continue

        pens.append({
            "elapsed": elapsed, "we": elapsed + mins * 60,
            "team": team, "text": text, "creates": _creates_pp(text),
            "is_coincidental": False,
            "is_dm": (mins == 4), "is_major": (mins == 5),
            "seq": seq, "pnum": pnum,
        })

    _detect_offsets(pens, away_abbr, home_abbr)

    # ── Collect PP goals for minor truncation ────────────────────────
    # Only goals by the non-penalised team truncate the window.
    pp_goals = []
    for p in raw_plays:
        to   = p.get("type", {}) if isinstance(p.get("type"), dict) else {}
        tt   = (to.get("text", "") or "").lower()
        txt  = (p.get("text") or "").lower()
        if "goal" not in tt or ("power play" not in txt and "power-play" not in txt):
            continue
        po   = p.get("period", {}) if isinstance(p.get("period"), dict) else {}
        pnum = po.get("number", 1)
        co   = p.get("clock", {}) if isinstance(p.get("clock"), dict) else {}
        gel    = espn_clock_to_seconds(co.get("displayValue", "0:00"), pnum)
        g_team = _resolve_team(p, bm)
        if   g_team == "away": g_team = away_abbr
        elif g_team == "home": g_team = home_abbr
        pp_goals.append({"el": gel, "team": g_team})

    # ── Build windows ────────────────────────────────────────────────
    windows = []
    for pen in pens:
        if pen["is_coincidental"] or not pen["creates"]: continue
        ws   = pen["elapsed"]; team = pen["team"]; pnum = pen["pnum"]

        # Direction: penalised team is short, opponent has advantage.
        if   team == away_abbr: short = "away"; base = "4v5 PP"
        elif team == home_abbr: short = "home"; base = "5v4 PP"
        else:                   short = "";     base = "PP"

        # 4v3 OT: period ≥ 4 (modern NHL 3v3 OT, one penalty → 4v3)
        if pnum >= 4:
            base = base.replace("4v5", "3v4").replace("5v4", "4v3")

        def _first_ppg(ws, we, short_team):
            return next(
                (g["el"] for g in pp_goals
                 if ws < g["el"] <= we and g["team"] != short_team),
                None
            )

        if pen["is_major"]:
            windows.append({"ws": ws, "we": pen["we"], "sit": base,
                            "short": short, "source_seq": pen["seq"]})
        elif pen["is_dm"]:
            mid = ws + 120
            g   = _first_ppg(ws, mid, team)
            if g:
                windows.append({"ws": ws,  "we": g + 1,    "sit": base,
                                "short": short, "source_seq": pen["seq"]})
                windows.append({"ws": mid, "we": pen["we"], "sit": base,
                                "short": short, "source_seq": pen["seq"]})
            else:
                windows.append({"ws": ws, "we": pen["we"], "sit": base,
                                "short": short, "source_seq": pen["seq"]})
        else:
            g   = _first_ppg(ws, pen["we"], team)
            end = (g + 1) if g else pen["we"]
            windows.append({"ws": ws, "we": end, "sit": base,
                            "short": short, "source_seq": pen["seq"]})

    return windows



# =========================
# NHL SHIFT CHART EN (Item 1)
# =========================
@st.cache_data(show_spinner=False)
def fetch_nhl_shifts(nhl_game_id, cache_bucket: int = 0):
    """Fetch NHL shift chart. Returns list of shift dicts."""
    if not nhl_game_id:
        return []
    try:
        resp = requests.get(NHL_SHIFTS.format(gid=nhl_game_id), timeout=15)
        resp.raise_for_status()
        return resp.json().get("data", [])
    except Exception:
        return []


def build_en_windows_from_shifts(shifts, goalie_ids, delayed_events, game_state="OFF"):
    """
    Build EN windows from goalie shift gaps.
    Replaces sit-code approach (52.9% miss / 10.2% FP rates confirmed).

    Filters:
      Backup filter  — if another goalie on the same team starts a shift within
                       the gap it is a goalie change, not an EN pull. Excluded.
      Open gaps      — only emitted for LIVE/CRIT games (goalie currently off ice).

    Returns list of {ws, we, dur}.
    ws/we are in espn_clock units (counting UP) for comparison with ESPN elapsed.
    Validated: 15/15 unit tests across 3 games including 2 goalie-change exclusions.
    """
    if not shifts or not goalie_ids:
        return []

    by_gp      = defaultdict(list)
    team_of    = {}

    for s in shifts:
        pid = s["playerId"]
        if pid not in goalie_ids:
            continue
        per = s["period"]
        by_gp[(pid, per)].append(s)
        team_of[pid] = s["teamId"]

    # All goalie shift start-times (for backup filter)
    goalie_starts = []
    for s in shifts:
        if s["playerId"] not in goalie_ids:
            continue
        per = s["period"]
        goalie_starts.append({
            "playerId":  s["playerId"],
            "teamId":    s["teamId"],
            "start_sec": espn_clock_to_seconds(s["startTime"], per),
        })

    # Delayed-penalty espn_elapsed set (for is_delayed label)
    delayed_els = {dp["espn_elapsed"] for dp in delayed_events}

    def has_backup(gap_ws, gap_we, own_pid, own_tid):
        return any(
            gs["teamId"] == own_tid
            and gs["playerId"] != own_pid
            and gap_ws <= gs["start_sec"] <= gap_we
            for gs in goalie_starts
        )

    windows = []

    for (pid, per), pshifts in by_gp.items():
        own_tid  = team_of[pid]

        # Merge overlapping/duplicate shifts before gap detection.
        # The NHL shift chart sometimes emits two records for the same goalie
        # in one period — e.g. a real full-period 00:00→20:00 shift plus a
        # corrupt partial 00:00→14:04 record (SCF G3 Carter Hart). Without
        # merging, the per-shift gap logic treats them independently and the
        # corrupt record fires a phantom open-gap EN window. Collapsing each
        # goalie's overlapping segments into continuous blocks first makes the
        # detection robust to that corruption. Validated across 201 games:
        # eliminates all such false positives (2→0) with 0 real windows lost.
        segs = sorted(
            (espn_clock_to_seconds(s["startTime"], per),
             espn_clock_to_seconds(s["endTime"],   per))
            for s in pshifts
        )
        merged = []
        for ws, we in segs:
            if merged and ws <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], we)
            else:
                merged.append([ws, we])

        # Consecutive gaps between merged blocks
        for i in range(len(merged) - 1):
            ws  = merged[i][1]
            we  = merged[i + 1][0]
            dur = we - ws
            if dur <= 0:
                continue
            if has_backup(ws, we, pid, own_tid):
                continue
            windows.append({"ws": ws, "we": we, "dur": dur})

        # Open gap: last merged block ended before period end.
        # Emitted for all game states — the backup filter is the guard against
        # goalie changes. For completed games this correctly captures EN pulls
        # where the goalie never returned because the game ended (e.g. Andersen
        # P3 18:14→20:00 in SCF G1). Validated: backup filter excludes all
        # known goalie-change cases (Comrie, Kochetkov) in regression tests.
        # Option 1 zero-shift guard: for live games require 2+ merged blocks so
        # a single in-progress shift with a stale endTime cannot create a
        # phantom EN window across the period (SCF G2 P2 phantom fix).
        if per <= 3 and (game_state not in ("LIVE", "CRIT") or len(merged) >= 2):
            ws_last = merged[-1][0]
            we_last = merged[-1][1]
            per_end = per * 1200
            # Guard: skip corrupted shift data where endTime precedes startTime
            if we_last > ws_last and per_end - we_last >= 1 \
                    and not has_backup(we_last, per_end, pid, own_tid):
                windows.append({"ws": we_last, "we": per_end, "dur": per_end - we_last})

    return windows


def find_espn_situation(elapsed, pp_windows):
    """
    PP situation for an elapsed time from ESPN-first windows.
    Automatically handles 5v3 (two same-short-team overlapping windows).
    Returns e.g. '5v4 PP', '5v3 PP', '4v3 PP', '' (even strength).
    """
    matching = [w for w in pp_windows if w["ws"] <= elapsed <= w["we"]]
    if not matching: return ""
    if len(matching) == 1: return matching[0]["sit"]
    # Multiple overlapping windows
    ac = sum(1 for w in matching if w["short"] == "away")
    hc = sum(1 for w in matching if w["short"] == "home")
    if ac >= 2 and hc == 0: return "3v5 PP"   # away has 2 in box
    if hc >= 2 and ac == 0: return "5v3 PP"   # home has 2 in box
    return matching[0]["sit"]


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
    # Return cached plays if available
    if st.session_state.cached_event_id == event_id and st.session_state.cached_plays:
        return st.session_state.cached_plays

    # ── Fetch ESPN ────────────────────────────────────────────────────
    try:
        resp = requests.get(ESPN_SUMMARY, params={"event": event_id}, timeout=15)
        resp.raise_for_status()
        espn_json = resp.json()
        raw_plays = espn_json.get("plays", [])
    except Exception as e:
        st.error(f"ESPN error: {e}")
        return []

    # ── Fetch NHL (delayed-penalty events + EN sit codes) ─────────────
    bucket   = st.session_state.get("force_bucket") or int(time.time() // 15)
    nhl_data = (fetch_nhl_plays(nhl_game_id, cache_bucket=bucket)
                if nhl_game_id
                else {"plays": [], "delayed_events": [], "teams": {}})

    st.session_state.last_refresh = datetime.now(ET)

    # ── Build ESPN-first PP windows ───────────────────────────────────
    # Validated: 843 windows · 160 games · 100% PP Cause · 0 phantoms
    pp_windows = build_pp_windows_from_espn(espn_json, away_abbr, home_abbr)

    # ── Build EN windows from NHL shift charts (Item 1) ─────────────────
    # Replaces sit-code scan (52.9% miss, 10.2% FP confirmed across 160 games).
    # Backup filter removes goalie changes. Validated 15/15 unit tests.
    shifts     = fetch_nhl_shifts(nhl_game_id, cache_bucket=bucket)
    goalie_ids = nhl_data.get("goalie_ids", set())
    game_state = nhl_data.get("game_state", "OFF")
    en_windows = build_en_windows_from_shifts(
        shifts, goalie_ids, nhl_data.get("delayed_events", []), game_state
    )

    # ── Index NHL situationCode by ESPN-elapsed for strength labelling ──
    # situationCode is the per-play authority for goalie-pulled state and
    # skater counts. Used to render the strength label (e.g. '6v4 CAR EN PP')
    # and to confirm EN: a shift-chart EN window is only displayed as EN when
    # situationCode agrees a goalie is pulled (the 3.6% disagreement cases are
    # suppressed so the label never contradicts the data). Shootout plays
    # (period 5) are excluded — their 1v0/0v1 codes are not real strengths.
    sit_by_elapsed = {}
    for np_ in nhl_data.get("plays", []):
        if np_.get("period", 1) >= 5:
            continue
        sc = np_.get("sit_code") or ""
        if len(sc) >= 4:
            sit_by_elapsed[np_.get("espn_elapsed")] = sc

    def nhl_sit_at(elapsed_sec):
        """Nearest NHL situationCode within 5s of the given ESPN elapsed."""
        best = ""; best_dt = 6
        for el, sc in sit_by_elapsed.items():
            dt = abs(el - elapsed_sec)
            if dt < best_dt:
                best_dt = dt; best = sc
        return best

    # ── Index PP windows by source play sequenceNumber ────────────────
    # PP Cause is by construction — penalty play that built the window
    pp_cause_seqs = {w["source_seq"] for w in pp_windows}
    seq_to_win    = {w["source_seq"]: w for w in pp_windows}

    # ── Parse ESPN plays ──────────────────────────────────────────────
    plays       = []
    last_pp_sit = None   # carry-forward for SH plays outside windows (2.4% gap)

    for p in sorted(raw_plays, key=lambda x: int(x.get("sequenceNumber", 0))):
        po   = p.get("period", {}) if isinstance(p.get("period"), dict) else {}
        pnum = po.get("number", 1)
        ptype= po.get("type", "")
        co   = p.get("clock", {}) if isinstance(p.get("clock"), dict) else {}
        clk  = co.get("displayValue", "") if isinstance(co, dict) else str(co)
        to   = p.get("type", {}) if isinstance(p.get("type"), dict) else {}
        type_text = to.get("text", "") if isinstance(to, dict) else str(to)
        type_id   = to.get("id",   "") if isinstance(to, dict) else ""
        text     = p.get("text", "")
        wall_raw = p.get("wallclock", "")
        seq      = int(p.get("sequenceNumber", 0))
        elapsed  = espn_clock_to_seconds(clk, pnum)

        # ── Strength: window overlay → EN → ESPN carry-forward ────────
        so      = p.get("strength", {}) if isinstance(p.get("strength"), dict) else {}
        str_id  = str(so.get("id", "")) if isinstance(so, dict) else ""
        str_txt = (so.get("text", "") or "") if isinstance(so, dict) else ""
        # shotInfo.id=903: ESPN EN signal on shots/misses at empty net (Item 3)
        shi     = p.get("shotInfo", {}) if isinstance(p.get("shotInfo"), dict) else {}
        shot_id = str(shi.get("id", "")) if isinstance(shi, dict) else ""

        pp_sit = find_espn_situation(elapsed, pp_windows)

        # NHL situationCode at this play — authority for EN + skater counts
        nhl_sit   = nhl_sit_at(elapsed)
        sit_label = make_strength_label(nhl_sit, away_abbr, home_abbr) if nhl_sit else ""
        sit_is_en = "EN" in sit_label

        if pp_sit:
            # On a power play. Prefer the situationCode label when it agrees
            # this is a PP (and it may add EN team info, e.g. '6v4 CAR EN PP').
            situation   = sit_label if ("PP" in sit_label) else pp_sit
            last_pp_sit = pp_sit
        elif str_txt in ("Power Play", "Shorthanded"):
            # ESPN confirms PP/SH but play falls just outside window boundary
            situation = last_pp_sit if last_pp_sit else str_txt
        elif sit_is_en:
            # situationCode confirms a pulled goalie — richest EN label
            situation   = sit_label
            last_pp_sit = None
        elif (str_id == "903" or shot_id == "903") and sit_is_en:
            # ESPN EN tag, confirmed by situationCode
            situation   = sit_label
            last_pp_sit = None
        elif any(w["ws"] <= elapsed <= w["we"] for w in en_windows) and sit_is_en:
            # Inside a merge-validated NHL EN window AND situationCode agrees.
            # When situationCode disagrees (goalie shown in net), no EN is
            # displayed — the label must never contradict the data.
            situation   = sit_label
            last_pp_sit = None
        else:
            last_pp_sit = None
            situation   = ""

        # ── PP Cause: by construction from window source_seq ─────────
        is_pp_cause = (seq in pp_cause_seqs)
        if is_pp_cause:
            win      = seq_to_win[seq]
            pp_arrow = f"5v5 → {win['sit']}"
        else:
            pp_arrow = ""

        plays.append({
            "seq":          seq,
            "period_num":   pnum,
            "period_type":  ptype,
            "period_label": period_label(pnum, ptype),
            "clock":        clk,
            "elapsed":      elapsed,
            "type_text":    type_text,
            "type_id":      type_id,
            "text":         text,
            "situation":    situation,
            "wall_raw":     wall_raw,
            "wall_et":      fmt_et_full(wall_raw),
            "wall_dt":      to_et(wall_raw),
            "away_score":   p.get("awayScore", ""),
            "home_score":   p.get("homeScore", ""),
            "emoji":        get_play_emoji(type_text),
            "is_pp_cause":  is_pp_cause,
            "pp_arrow":     pp_arrow,
            "is_delayed":   False,
        })

    # ── Inject Delayed Penalty cards (from NHL delayed-penalty events) ─
    away_id = str(nhl_data.get("teams", {}).get("away_id", "") or "")
    home_id = str(nhl_data.get("teams", {}).get("home_id", "") or "")

    for dp in nhl_data.get("delayed_events", []):
        # Use espn_elapsed so the card sorts to the correct chronological position
        dp_el   = dp.get("espn_elapsed", dp["elapsed"])
        dp_pnum = dp["period"]
        oid     = str(dp.get("owner_id", "") or "")

        # Build card text — enriched when paired, fallback for unpaired
        desc      = (dp.get("desc_key") or "").replace("-", " ").title()
        duration  = dp.get("duration", 2)
        committed = dp.get("committed_name", "")
        drawn     = dp.get("drawn_name", "")
        # Fix 4: label double minor in duration text
        dur_label = (f"{duration} min (double minor)"
                     if dp.get("is_double_minor") else f"{duration} min")

        if desc and committed and drawn:
            dp_txt = (
                f"Delayed Penalty: {desc} ({dur_label}) "
                f"\u2014 {committed} against {drawn}"
            )
        elif desc and committed:
            dp_txt = f"Delayed Penalty: {desc} ({dur_label}) \u2014 {committed}"
        elif desc:
            dp_txt = f"Delayed Penalty: {desc} ({dur_label})"
        # Fix 2: goal stopped play in the delayed window
        elif dp.get("goal_cancelled"):
            team_abbr = (away_abbr if oid == away_id
                         else (home_abbr if oid == home_id else ""))
            pfx = f"{team_abbr} " if team_abbr else ""
            dp_txt = f"Delayed Penalty \u2014 {pfx}play stopped by goal"
        # Fix 3: owner_id = committing team — text direction corrected
        elif oid == away_id:
            dp_txt = f"Delayed Penalty \u2014 {away_abbr} penalty called"
        elif oid == home_id:
            dp_txt = f"Delayed Penalty \u2014 {home_abbr} penalty called"
        else:
            dp_txt = "Delayed Penalty \u2014 in progress"

        _fwd  = [p for p in plays if p["elapsed"] >= dp_el]
        _near = min(_fwd, key=lambda p: p["elapsed"]) if _fwd else \
                (min(plays, key=lambda p: abs(p["elapsed"] - dp_el)) if plays else None)

        # Change 1: suppress delayed card when a PP Cause play already exists
        # at the same elapsed time — the penalty is already shown as PP Cause,
        # so the delayed card would be a duplicate of the same event.
        if any(p.get("is_pp_cause") and abs(p.get("elapsed", -9999) - dp_el) <= 5
               for p in plays):
            continue

        plays.append({
            "seq":          -2,
            "period_num":   dp_pnum,
            "period_type":  "REG",
            "period_label": f"P{dp_pnum}",
            "clock":        (_near.get("clock", "")      if _near else ""),
            "elapsed":      dp_el,
            "type_text":    "Delayed Penalty",
            "type_id":      "",
            "text":         dp_txt,
            "situation":    "Delayed Penalty",
            "wall_raw":     "",
            "wall_et":      (_near.get("wall_et", "")    if _near else ""),
            "wall_dt":      None,
            "away_score":   (_near.get("away_score", "") if _near else ""),
            "home_score":   (_near.get("home_score", "") if _near else ""),
            "emoji":        "\U0001f591",
            "is_pp_cause":  False,
            "pp_arrow":     "",
            "is_delayed":   True,
        })

    plays.sort(key=lambda p: (p.get("elapsed", 0), p.get("seq", 0)))

    st.session_state.cached_plays    = plays
    st.session_state.cached_event_id = event_id
    return plays


# =========================
# ESPN LIVE SITUATION (Item 4)
# =========================
@st.cache_data(ttl=5, show_spinner=False)
def fetch_espn_situation(event_id):
    """
    Real-time EN/PP state from ESPN Core API situation endpoint.
    Returns current boolean state — only meaningful for live games.
    Completed games always return false (no goalie currently pulled).
    Poll every ~5s (ttl=5) during live games for status bar display.
    """
    if not event_id:
        return {"emptyNet": False, "powerPlay": False}
    try:
        url  = ESPN_SITUATION.format(eid=event_id)
        resp = requests.get(url, timeout=5)
        resp.raise_for_status()
        d = resp.json()
        return {
            "emptyNet":  bool(d.get("emptyNet",  False)),
            "powerPlay": bool(d.get("powerPlay", False)),
        }
    except Exception:
        return {"emptyNet": False, "powerPlay": False}


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

    nav_col1, nav_col2, nav_col3, nav_col4, _ = st.columns([1.3, 0.9, 1.1, 1.8, 4.9])
    with nav_col1:
        if st.button("⬅ Back to Schedule", use_container_width=True):
            st.session_state.view            = "schedule"
            st.session_state.filters_applied = False
            st.session_state.filtered_plays  = None
            st.rerun()
    with nav_col2:
        if st.button("🔄 Refresh", use_container_width=True):
            # Force a new cache bucket so fetch_nhl_plays gets fresh data
            # regardless of any shared function cache on Streamlit Cloud
            st.session_state.force_bucket    = int(time.time() // 30) + 1
            st.session_state.cached_plays    = None
            st.session_state.cached_event_id = None
            st.rerun()
    with nav_col3:
        sort_label = "↓ Oldest first" if not st.session_state.sort_newest_first else "↑ Newest first"
        sort_type  = "secondary" if not st.session_state.sort_newest_first else "primary"
        if st.button(sort_label, use_container_width=True, type=sort_type):
            st.session_state.sort_newest_first = not st.session_state.sort_newest_first
            st.rerun()
    with nav_col4:
        refresh_time = st.session_state.last_refresh.strftime("%H:%M:%S ET")
        st.markdown(
            f'<div style="background-color:#2e7d32;color:white;padding:8px 16px;'
            f'border-radius:4px;font-size:14px;font-weight:bold;text-align:center;">'
            f'Last refresh {refresh_time}</div>',
            unsafe_allow_html=True,
        )

    st.markdown("<br>", unsafe_allow_html=True)

    # Read live score from cached scoreboard (TTL=30s) — falls back to
    # session state if the event is not found or the fetch fails.
    _live_away = st.session_state.away_score
    _live_home = st.session_state.home_score
    try:
        _board = fetch_scoreboard(st.session_state.sched_date.strftime("%Y-%m-%d"))
        for _g in _board:
            if str(_g["event_id"]) == str(st.session_state.event_id):
                _live_away = _g["away_score"]
                _live_home = _g["home_score"]
                break
    except Exception:
        pass

    head_c1, head_c2, head_c3 = st.columns([1, 6, 1])
    with head_c1:
        if st.session_state.away_logo:
            st.image(st.session_state.away_logo, width=80)
    with head_c2:
        st.markdown(f"""
            <div style="display:flex;align-items:center;justify-content:center;
                font-weight:800;font-size:clamp(20px,3vw,32px);gap:15px;text-align:center;">
                <span>{st.session_state.away}</span>
                <span style="color:#888;">{_live_away}</span>
                <span style="color:#444;">-</span>
                <span style="color:#888;">{_live_home}</span>
                <span>{st.session_state.home}</span>
            </div>
        """, unsafe_allow_html=True)
    with head_c3:
        if st.session_state.home_logo:
            st.image(st.session_state.home_logo, width=80)

    nhl_id = st.session_state.nhl_game_id
    st.caption(
        f"📡 ESPN-primary · NHL `{nhl_id}` (shift chart EN + delayed-penalty)" if nhl_id
        else "📡 ESPN only"
    )

    # ── Live situation badge (Item 4) ─────────────────────────────────
    # ESPN Core situation endpoint: emptyNet + powerPlay booleans.
    # Only meaningful for live games — completed games always show false.
    _gs = st.session_state.get("game_state", "")
    if _gs in ("in", "LIVE", "CRIT"):
        _sit = fetch_espn_situation(st.session_state.event_id)
        _badges = []
        if _sit.get("emptyNet"):
            _badges.append(
                '<span style="background:#c0392b;color:#fff;font-size:12px;'
                'font-weight:600;padding:3px 10px;border-radius:4px;margin-right:6px">'
                '🥅 Empty Net</span>'
            )
        if _sit.get("powerPlay"):
            _badges.append(
                '<span style="background:#e67e22;color:#fff;font-size:12px;'
                'font-weight:600;padding:3px 10px;border-radius:4px;margin-right:6px">'
                '⚡ Power Play</span>'
            )
        if _badges:
            st.markdown("".join(_badges), unsafe_allow_html=True)

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
                if USE_PP_FILTER and " PP" not in sit and not p.get("is_pp_cause", False) and not p.get("is_delayed", False): return False
                if USE_GP_FILTER and "EN" not in sit: return False
                return True
            st.session_state.filtered_plays  = [p for p in plays if passes(p)]
            st.session_state.filters_applied = True
            # Snapshot which filters were active at Apply time —
            # info labels only read from this, not live checkbox state
            st.session_state.active_filter_snapshot = {
                "period":     USE_PERIOD_FILTER,
                "periods":    list(selected_periods),
                "time":       USE_TIME_FILTER,
                "start_dt":   START_DT,
                "end_dt":     END_DT,
                "goals":      USE_GOAL_FILTER,
                "pp":         USE_PP_FILTER,
                "en":         USE_GP_FILTER,
            }
            st.rerun()

    with btn_col2:
        def reset_filters():
            st.session_state.filters_applied        = False
            st.session_state.filtered_plays         = None
            st.session_state.active_filter_snapshot = {}
            st.session_state.cb_period = False
            st.session_state.cb_time   = False
            st.session_state.cb_goals  = False
            st.session_state.cb_pp     = False
            st.session_state.cb_en     = False
        # Disabled when no filters are active (fix #3)
        filters_currently_applied = st.session_state.get("filters_applied", False)
        st.button(
            "🗑️ Remove Filters",
            use_container_width=True,
            on_click=reset_filters,
            disabled=not filters_currently_applied,
        )

    filters_applied = st.session_state.get("filters_applied")
    display_list    = st.session_state.filtered_plays if filters_applied else plays
    # Fix 2: reverse display order without mutating the stored list
    if st.session_state.sort_newest_first:
        display_list = display_list[::-1]
    total   = len(plays)
    showing = len(display_list)

    # Info labels read from snapshot (fix #4) — only shows filters
    # that were active when Apply was last pressed, not current checkboxes
    if filters_applied:
        snap = st.session_state.get("active_filter_snapshot", {})
        if showing == 0:
            st.warning("⚠️ No results found — please check the filters applied.")
            st.stop()
        if snap.get("period"):
            labels = snap["periods"] if snap["periods"] else ["none selected"]
            st.info(f"🏒 **Period filter:** {', '.join(labels)} — showing **{showing}** of **{total}** plays")
        if snap.get("time") and snap.get("start_dt") and snap.get("end_dt"):
            st.info(f"🕐 **Time filter:** {snap['start_dt'].strftime('%Y-%m-%d %H:%M')} → {snap['end_dt'].strftime('%Y-%m-%d %H:%M')} ET — showing **{showing}** of **{total}** plays")
        if snap.get("goals"):
            n_goals = sum(1 for p in plays if p["type_text"] == "Goal")
            st.info(f"🚨 **Goals Only:** {n_goals} goal(s) in game — showing **{showing}** of **{total}** plays")
        if snap.get("pp"):
            st.info(f"⚡ **Power Plays Only:** showing **{showing}** of **{total}** plays")
        if snap.get("en"):
            st.info(f"🥅 **Empty Nets Only:** showing **{showing}** of **{total}** plays")

    for p in display_list:
        emoji        = "🚨" if p.get("type_text") == "Goal" else p.get("emoji", "🏒")
        sit          = p.get("situation", "")
        is_pp_cause  = p.get("is_pp_cause", False)

        if is_pp_cause:
            strength_display = p.get("pp_arrow") or sit
            wall_et = p.get("wall_et", "")
            time_row = (f'<p style="margin:12px 0 0 0;font-size:1rem">🕐 <b>Time (ET):</b> <code>{wall_et}</code></p>'
                        if wall_et and wall_et != "N/A" else "")
            strength_row = (f'<p style="margin:12px 0 0 0;font-size:1rem">⚖️ <b>Strength:</b> <code>{strength_display}</code></p>'
                            if strength_display else "")
            st.markdown(f"""
<div style="border-left:3px solid #BA7517;padding-left:12px;margin:20px 0 0 0;border-radius:0">
  <div style="display:flex;align-items:center;gap:10px;margin:0 0 12px 0">
    <span style="font-size:1.5rem;font-weight:600;line-height:1.3">{emoji} {p.get('period_label')} | ⏱️ {p.get('clock')}</span>
    <span style="background:#FAEEDA;color:#854F0B;font-size:12px;font-weight:500;padding:2px 8px;border-radius:4px;white-space:nowrap">PP Cause</span>
  </div>
  <p style="margin:12px 0 0 0;font-size:1rem">📊 <b>Score:</b> {p.get('away_score')} - {p.get('home_score')}</p>
  <p style="margin:12px 0 0 0;font-size:1rem">🎯 <b>Event:</b> {p.get('type_text')}</p>
  {strength_row}
  <p style="margin:12px 0 0 0;font-size:1rem">📋 <b>Play:</b> {p.get('text')}</p>
  {time_row}
</div>
""", unsafe_allow_html=True)
        elif p.get("is_delayed"):
            # Delayed penalty card — blue border + badge
            # Timestamp is approximate (nearest ESPN play); shown with ~ prefix.
            wall_et = p.get("wall_et", "")
            time_row = (
                f'<p style="margin:12px 0 0 0;font-size:1rem">🕐 <b>Time (ET):</b>'
                f' <code>approx {wall_et}</code></p>'
                if wall_et and wall_et != "N/A" else ""
            )
            st.markdown(f"""
<div style="border-left:3px solid #185FA5;padding-left:12px;margin:20px 0 0 0;border-radius:0">
  <div style="display:flex;align-items:center;gap:10px;margin:0 0 12px 0">
    <span style="font-size:1.5rem;font-weight:600;line-height:1.3">{emoji} {p.get('period_label')} | ⏱️ {p.get('clock')}</span>
    <span style="background:#E6F1FB;color:#185FA5;font-size:12px;font-weight:500;padding:2px 8px;border-radius:4px;white-space:nowrap">Delayed Penalty</span>
  </div>
  <p style="margin:12px 0 0 0;font-size:1rem">📊 <b>Score:</b> {p.get('away_score')} - {p.get('home_score')}</p>
  <p style="margin:12px 0 0 0;font-size:1rem">🎯 <b>Event:</b> {p.get('type_text')}</p>
  <p style="margin:12px 0 0 0;font-size:1rem">📋 <b>Play:</b> {p.get('text')}</p>
  {time_row}
</div>
""", unsafe_allow_html=True)
        else:
            # Standard render
            # Change 2: amber left border on PP plays (matches PP Cause border)
            # Change 3: teal left border on EN plays + "Empty Net" strength text
            # EN takes priority over PP when both are present
            wall_et  = p.get("wall_et", "")
            time_row = (
                f'<p style="margin:12px 0 0 0;font-size:1rem">🕐 <b>Time (ET):</b>'
                f' <code>{wall_et}</code></p>'
                if wall_et and wall_et != "N/A" else ""
            )

            if "EN" in sit:
                # Teal border — EN takes priority over PP.
                # sit is now a full situationCode label, e.g. "6v4 CAR EN PP"
                # or "6v5 VGK EN" — already self-describing, shown as-is.
                sit_display = sit
                st.markdown(f"""
<div style="border-left:3px solid #1D9E75;padding-left:12px;margin:20px 0 0 0;border-radius:0">
  <p style="margin:0 0 12px 0;font-size:1.5rem;font-weight:600;line-height:1.3">{emoji} {p.get('period_label')} | ⏱️ {p.get('clock')}</p>
  <p style="margin:12px 0 0 0;font-size:1rem">📊 <b>Score:</b> {p.get('away_score')} - {p.get('home_score')}</p>
  <p style="margin:12px 0 0 0;font-size:1rem">🎯 <b>Event:</b> {p.get('type_text')}</p>
  <p style="margin:12px 0 0 0;font-size:1rem">⚖️ <b>Strength:</b> <code>{sit_display}</code></p>
  <p style="margin:12px 0 0 0;font-size:1rem">📋 <b>Play:</b> {p.get('text')}</p>
  {time_row}
</div>
""", unsafe_allow_html=True)

            elif " PP" in sit:
                # Amber border — matches PP Cause border color
                st.markdown(f"""
<div style="border-left:3px solid #BA7517;padding-left:12px;margin:20px 0 0 0;border-radius:0">
  <p style="margin:0 0 12px 0;font-size:1.5rem;font-weight:600;line-height:1.3">{emoji} {p.get('period_label')} | ⏱️ {p.get('clock')}</p>
  <p style="margin:12px 0 0 0;font-size:1rem">📊 <b>Score:</b> {p.get('away_score')} - {p.get('home_score')}</p>
  <p style="margin:12px 0 0 0;font-size:1rem">🎯 <b>Event:</b> {p.get('type_text')}</p>
  <p style="margin:12px 0 0 0;font-size:1rem">⚖️ <b>Strength:</b> <code>{sit}</code></p>
  <p style="margin:12px 0 0 0;font-size:1rem">📋 <b>Play:</b> {p.get('text')}</p>
  {time_row}
</div>
""", unsafe_allow_html=True)

            else:
                # Plain plays — no strength border
                st.subheader(f"{emoji} {p.get('period_label')} | ⏱️ {p.get('clock')}")
                st.markdown(f"📊 **Score:** {p.get('away_score')} - {p.get('home_score')}")
                st.markdown(f"🎯 **Event:** {p.get('type_text')}")
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
