import requests
import base64
from urllib.parse import urlencode


class YahooClient:
    BASE_URL = "https://fantasysports.yahooapis.com/fantasy/v2"
    AUTH_URL = "https://api.login.yahoo.com/oauth2/request_auth"
    TOKEN_URL = "https://api.login.yahoo.com/oauth2/get_token"

    def __init__(self, client_id, client_secret):
        self.client_id = client_id
        self.client_secret = client_secret

    # ── OAuth ──────────────────────────────────────────────────────────────────

    def get_auth_url(self, redirect_uri):
        params = {
            "client_id": self.client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "language": "en-us",
        }
        return f"{self.AUTH_URL}?{urlencode(params)}"

    def _token_request(self, data):
        creds = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        resp = requests.post(
            self.TOKEN_URL,
            headers={
                "Authorization": f"Basic {creds}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data=data,
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()

    def exchange_code(self, code, redirect_uri):
        return self._token_request(
            {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri}
        )

    def refresh_token(self, refresh_token):
        return self._token_request(
            {"grant_type": "refresh_token", "refresh_token": refresh_token}
        )

    # ── HTTP ───────────────────────────────────────────────────────────────────

    def _get(self, path, token, extra_params=None):
        params = {"format": "json"}
        if extra_params:
            params.update(extra_params)
        resp = requests.get(
            f"{self.BASE_URL}{path}",
            headers={"Authorization": f"Bearer {token}"},
            params=params,
            timeout=20,
        )
        resp.raise_for_status()
        return resp.json()

    # ── Parsing helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _parse_player_meta(raw_list):
        """Flatten Yahoo's metadata list into a simple dict."""
        meta = {}
        for item in raw_list:
            if not isinstance(item, dict):
                continue
            for k, v in item.items():
                if k == "name" and isinstance(v, dict):
                    meta["full_name"] = v.get("full", "")
                elif k == "selected_position" and isinstance(v, list):
                    for sp in v:
                        if isinstance(sp, dict) and "position" in sp:
                            meta["selected_position"] = sp["position"]
                else:
                    meta[k] = v
        return meta

    @staticmethod
    def _parse_stats(stats_block):
        """Convert Yahoo stats list-of-dicts into {stat_id: value}."""
        out = {}
        if not isinstance(stats_block, dict):
            return out
        for i in range(stats_block.get("count", 0)):
            stat = stats_block.get(str(i), {}).get("stat", {})
            sid = stat.get("stat_id")
            if sid:
                out[sid] = stat.get("value", "-")
        return out

    # ── Public API methods ─────────────────────────────────────────────────────

    def get_leagues(self, token):
        data = self._get("/users;use_login=1/games;game_keys=nhl/leagues", token)
        leagues = []
        try:
            games = data["fantasy_content"]["users"]["0"]["user"][1]["games"]
            for i in range(games["count"]):
                game = games[str(i)]["game"]
                if game[0].get("code") != "nhl":
                    continue
                league_data = game[1].get("leagues", {})
                for j in range(league_data.get("count", 0)):
                    l = league_data[str(j)]["league"][0]
                    leagues.append(
                        {
                            "league_key": l["league_key"],
                            "name": l["name"],
                            "num_teams": l.get("num_teams"),
                            "current_week": l.get("current_week"),
                            "season": game[0].get("season"),
                            "logo_url": l.get("logo_url", ""),
                            "scoring_type": l.get("scoring_type", ""),
                        }
                    )
        except (KeyError, TypeError, IndexError):
            pass
        return leagues

    def get_league_info(self, token, league_key):
        data = self._get(f"/league/{league_key}", token)
        return data["fantasy_content"]["league"][0]

    def get_stat_categories(self, token, league_key):
        """Return {stat_id: {name, display_name, sort_order}} for scored cats."""
        data = self._get(f"/league/{league_key}/settings", token)
        cats = {}
        try:
            settings = data["fantasy_content"]["league"][1]["settings"][0]
            stats_data = settings.get("stat_categories", {}).get("stats", {})
            for i in range(stats_data.get("count", 0)):
                s = stats_data[str(i)]["stat"]
                if s.get("is_only_display_stat") == "1":
                    continue
                cats[s["stat_id"]] = {
                    "name": s["name"],
                    "display_name": s.get("display_name", s["name"]),
                    "sort_order": s.get("sort_order", "1"),  # 1=higher better, 0=lower better
                }
        except (KeyError, TypeError):
            pass
        return cats

    def get_my_team(self, token, league_key):
        data = self._get(f"/league/{league_key}/teams", token)
        try:
            teams = data["fantasy_content"]["league"][1]["teams"]
            for i in range(teams["count"]):
                team_raw = teams[str(i)]["team"]
                meta = self._parse_player_meta(team_raw[0])
                if meta.get("is_owned_by_current_login") == 1:
                    return {
                        "team_key": meta.get("team_key"),
                        "team_id": meta.get("team_id"),
                        "name": meta.get("name"),
                        "waiver_priority": meta.get("waiver_priority"),
                        "number_of_moves": meta.get("number_of_moves"),
                        "number_of_trades": meta.get("number_of_trades"),
                        "logo_url": "",
                    }
        except (KeyError, TypeError, IndexError):
            pass
        return None

    def get_current_matchup(self, token, team_key, week):
        """Return matchup dict with both teams' category stats."""
        data = self._get(f"/team/{team_key}/matchups;weeks={week}", token)
        try:
            matchup_raw = data["fantasy_content"]["team"][1]["matchups"]["0"]["matchup"]
            teams_raw = matchup_raw.get("0", {}).get("teams", {})
            result = {
                "week": matchup_raw.get("week"),
                "status": matchup_raw.get("status"),
                "winner_team_key": matchup_raw.get("winner_team_key"),
                "is_tied": matchup_raw.get("is_tied"),
                "teams": [],
            }
            for i in range(2):
                team_data = teams_raw.get(str(i), {}).get("team", [])
                meta = self._parse_player_meta(team_data[0])
                stats = {}
                total_points = "0"
                if len(team_data) > 1:
                    stats = self._parse_stats(
                        team_data[1].get("team_stats", {}).get("stats", {})
                    )
                    total_points = team_data[1].get("team_points", {}).get("total", "0")
                result["teams"].append(
                    {
                        "team_key": meta.get("team_key"),
                        "name": meta.get("name"),
                        "is_mine": meta.get("is_owned_by_current_login") == 1,
                        "stats": stats,
                        "total_points": total_points,
                    }
                )
            return result
        except (KeyError, TypeError, IndexError):
            return None

    def get_roster_stats(self, token, team_key, week, stat_type="week"):
        """Return list of player dicts with stats for given week."""
        path = f"/team/{team_key}/roster/players/stats;type={stat_type};week={week}"
        data = self._get(path, token)
        players = []
        try:
            p_data = data["fantasy_content"]["team"][1]["roster"]["0"]["players"]
            for i in range(p_data.get("count", 0)):
                p_raw = p_data[str(i)]["player"]
                meta = self._parse_player_meta(p_raw[0])
                stats = {}
                if len(p_raw) > 1:
                    stats = self._parse_stats(
                        p_raw[1].get("player_stats", {}).get("stats", {})
                    )
                players.append(
                    {
                        "player_key": meta.get("player_key"),
                        "full_name": meta.get("full_name", ""),
                        "position": meta.get("display_position", ""),
                        "selected_position": meta.get("selected_position", "BN"),
                        "team_abbr": meta.get("editorial_team_abbr", ""),
                        "status": meta.get("status", ""),
                        "stats": stats,
                    }
                )
        except (KeyError, TypeError, IndexError):
            pass
        return players

    def get_season_roster_stats(self, token, team_key):
        """Return season stats for all players on roster."""
        data = self._get(f"/team/{team_key}/roster/players/stats;type=season", token)
        stats_by_key = {}
        try:
            p_data = data["fantasy_content"]["team"][1]["roster"]["0"]["players"]
            for i in range(p_data.get("count", 0)):
                p_raw = p_data[str(i)]["player"]
                meta = self._parse_player_meta(p_raw[0])
                pk = meta.get("player_key")
                stats = {}
                if len(p_raw) > 1:
                    stats = self._parse_stats(
                        p_raw[1].get("player_stats", {}).get("stats", {})
                    )
                if pk:
                    stats_by_key[pk] = stats
        except (KeyError, TypeError, IndexError):
            pass
        return stats_by_key

    def get_matchup_rosters(self, token, league_key, team_key, week):
        """Full matchup detail: category stats + both rosters."""
        matchup = self.get_current_matchup(token, team_key, week)
        if not matchup:
            return None

        stat_cats = self.get_stat_categories(token, league_key)

        for team in matchup["teams"]:
            tk = team["team_key"]
            try:
                team["players"] = self.get_roster_stats(token, tk, week)
            except Exception:
                team["players"] = []

        matchup["stat_categories"] = stat_cats
        return matchup

    def get_free_agents(self, token, league_key, sort="AR", count=30, position=""):
        """Top free agents with projected-week stats."""
        filters = f";status=FA;sort={sort};count={count}"
        if position:
            filters += f";position={position}"
        path = f"/league/{league_key}/players{filters}/stats;type=projected_week"
        data = self._get(path, token)
        players = []
        try:
            p_data = data["fantasy_content"]["league"][1]["players"]
            for i in range(p_data.get("count", 0)):
                p_raw = p_data[str(i)]["player"]
                meta = self._parse_player_meta(p_raw[0])
                stats = {}
                pct_owned = "0"
                pct_delta = "0"
                for segment in p_raw[1:]:
                    if isinstance(segment, dict):
                        if "player_stats" in segment:
                            stats = self._parse_stats(
                                segment["player_stats"].get("stats", {})
                            )
                        if "percent_owned" in segment:
                            pct_owned = segment["percent_owned"].get("value", "0")
                            pct_delta = segment["percent_owned"].get("delta", "0")
                players.append(
                    {
                        "player_key": meta.get("player_key", ""),
                        "full_name": meta.get("full_name", ""),
                        "position": meta.get("display_position", ""),
                        "team_abbr": meta.get("editorial_team_abbr", ""),
                        "status": meta.get("status", ""),
                        "stats": stats,
                        "pct_owned": pct_owned,
                        "pct_delta": pct_delta,
                    }
                )
        except (KeyError, TypeError, IndexError):
            pass
        return players
