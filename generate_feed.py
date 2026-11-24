import json
import os
import re
import urllib.parse
import urllib.request
from datetime import datetime, timezone

UA = "GameLifecycle-FeedBot/1.0"


def http_get_text(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def http_get_json(url, timeout=15):
    return json.loads(http_get_text(url, timeout=timeout))


def norm_title(raw):
    s = (raw or "").lower()
    s = s.replace("\u2122", " ").replace("\u00ae", " ").replace("\u00a9", " ")
    s = s.replace("&", " and ")
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def strip_edition(norm):
    for suf in (
        " standard edition",
        " deluxe edition",
        " gold edition",
        " ultimate edition",
        " complete edition",
        " definitive edition",
        " goty edition",
        " game of the year edition",
        " edition",
    ):
        if norm.endswith(suf):
            norm = norm[: -len(suf)].strip()
    return norm


def fetch_steam_app(app_id):
    data = http_get_json(f"https://api.steamcmd.net/v1/info/{app_id}")
    depots_obj = data.get("data", {}).get(str(app_id), {}).get("depots", {})
    if not isinstance(depots_obj, dict):
        return None

    branches = {}
    for bname, bval in (depots_obj.get("branches") or {}).items():
        if isinstance(bval, dict) and bval.get("buildid"):
            tu = 0
            try:
                tu = int(str(bval.get("timeupdated", "0")).strip())
            except ValueError:
                tu = 0
            branches[bname] = {
                "buildId": str(bval["buildid"]).strip(),
                "timeUpdated": tu,
            }

    depots = {}
    for dkey, dval in depots_obj.items():
        if not str(dkey).isdigit() or not isinstance(dval, dict):
            continue
        manifests = {}
        for mbranch, mval in (dval.get("manifests") or {}).items():
            gid = mval.get("gid") if isinstance(mval, dict) else mval
            if gid:
                manifests[mbranch] = str(gid).strip()
        if manifests:
            depots[str(dkey)] = manifests

    return {"branches": branches, "depots": depots} if branches else None


def fetch_epic_item(catalog_item_id):
    for path in (f"/v1/items/{catalog_item_id}/builds", f"/items/{catalog_item_id}/builds"):
        try:
            raw = http_get_json("https://api.egdata.app" + path)
            arr = raw if isinstance(raw, list) else (raw.get("elements") or raw.get("builds") or raw.get("data") or [])
            best = None
            best_dt = ""
            for entry in arr:
                if not isinstance(entry, dict):
                    continue
                ver = (entry.get("buildVersion") or entry.get("labelName") or entry.get("version") or "").strip()
                if not ver:
                    continue
                plat = (entry.get("platform") or "").lower()
                label = (entry.get("labelName") or "").lower()
                if any(x in plat for x in ("mac", "linux", "android")) or "-mac" in label:
                    continue
                dt = str(entry.get("updatedAt") or entry.get("createdAt") or "")
                if best is None or dt > best_dt:
                    best = entry
                    best_dt = dt
            if best:
                return {
                    "buildVersion": (best.get("buildVersion") or best.get("labelName") or best.get("version") or "").strip(),
                    "downloadSizeBytes": int(best.get("downloadSizeBytes") or 0),
                }
        except Exception:
            continue
    return None


def fetch_riot_product(product, region="eu"):
    url = f"https://clientconfig.rpg.riotgames.com/api/v1/config/public?namespace=keystone.products.{product}.patchlines"
    root = http_get_json(url)
    reg = (region or "eu").lower()
    fallback = None
    for k, v in root.items():
        if ".live" not in k.lower() or not isinstance(v, dict):
            continue
        configs = (v.get("platforms") or {}).get("win", {}).get("configurations") or []
        for cfg in configs:
            cid = str(cfg.get("id") or "").lower()
            purl = str(cfg.get("patch_url") or "")
            m = re.search(r"/([0-9A-Fa-f]{16})\.manifest(?:\?|$)", purl)
            if not m:
                continue
            mid = m.group(1).upper()
            if cid == reg or cid.startswith(reg) or (reg == "eu" and cid in ("euw", "eu1", "europe")):
                return {"manifestId": mid, "region": cid}
            if fallback is None:
                fallback = {"manifestId": mid, "region": cid}
    return fallback


def fetch_battlenet_product(product, region="eu"):
    reg = (region or "eu").lower()
    for url in (
        f"https://eu.version.battle.net/v2/products/{product}/versions",
        f"https://us.version.battle.net/v2/products/{product}/versions",
    ):
        try:
            text = http_get_text(url)
            lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]
            if len(lines) < 2:
                continue
            headers = [h.split("!")[0].strip().lower() for h in lines[0].split("|")]
            r_idx = headers.index("region") if "region" in headers else -1
            b_idx = headers.index("buildconfig") if "buildconfig" in headers else -1
            v_idx = headers.index("versionsname") if "versionsname" in headers else -1
            if b_idx < 0:
                continue
            fallback = None
            for ln in lines[1:]:
                cols = ln.split("|")
                if len(cols) <= b_idx:
                    continue
                bcfg = cols[b_idx].strip()
                vname = cols[v_idx].strip() if 0 <= v_idx < len(cols) else ""
                row_reg = cols[r_idx].strip().lower() if 0 <= r_idx < len(cols) else ""
                if not bcfg:
                    continue
                item = {"buildConfig": bcfg, "versionName": vname}
                if row_reg == reg:
                    return item
                if fallback is None:
                    fallback = item
            if fallback:
                return fallback
        except Exception:
            continue
    return None


def fetch_gog_product(game_id):
    data = http_get_json(f"https://content-system.gog.com/products/{game_id}/os/windows/builds?generation=2")
    items = data.get("items") or []
    if not items:
        return None
    first = items[0]
    bid = str(first.get("build_id") or "").strip()
    return {"buildId": bid, "versionName": str(first.get("version_name") or "").strip()} if bid else None


def resolve_steam_appid(title):
    target_norm = norm_title(title)
    base_target = strip_edition(target_norm)
    q = urllib.parse.quote(re.sub(r"[\u2122\u00ae\u00a9]", "", title).strip())
    data = http_get_json(f"https://store.steampowered.com/api/storesearch/?term={q}&l=english&cc=US")
    partial = None
    for item in data.get("items") or []:
        if item.get("type") and item["type"].lower() != "app":
            continue
        cn = norm_title(item.get("name", ""))
        if any(bad in cn for bad in ("soundtrack", " artbook", " demo", " test server", " beta", " dlc")):
            continue
        cid = str(item.get("id") or "")
        if not cid.isdigit():
            continue
        if cn == target_norm or strip_edition(cn) == base_target:
            return cid
        if partial is None and len(base_target) >= 8 and (cn.startswith(base_target + " ") or base_target.startswith(cn + " ")):
            partial = cid
    return partial


def main():
    with open("tracked_games.json", "r", encoding="utf-8") as f:
        tracked = json.load(f)

    out = {
        "schemaVersion": 1,
        "generatedAtUtc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "steam": {},
        "epic": {},
        "riot": {},
        "battlenet": {},
        "gog": {},
        "oracle": {},
    }

    for entry in tracked.get("steam", []):
        aid = str(entry.get("appId") or "").strip()
        if not aid:
            continue
        try:
            res = fetch_steam_app(aid)
            if res:
                out["steam"][aid] = res
        except Exception as e:
            print(f"[WARN] steam {aid}: {e}")

    for entry in tracked.get("epic", []):
        cid = str(entry.get("catalogItemId") or "").strip()
        if not cid:
            continue
        try:
            res = fetch_epic_item(cid)
            if res:
                out["epic"][cid] = res
        except Exception as e:
            print(f"[WARN] epic {cid}: {e}")

    for entry in tracked.get("riot", []):
        prod = str(entry.get("product") or "").strip().lower()
        reg = str(entry.get("region") or "eu").strip().lower()
        if not prod:
            continue
        try:
            res = fetch_riot_product(prod, reg)
            if res:
                out["riot"][prod] = res
        except Exception as e:
            print(f"[WARN] riot {prod}: {e}")

    for entry in tracked.get("battlenet", []):
        prod = str(entry.get("product") or "").strip().lower()
        reg = str(entry.get("region") or "eu").strip().lower()
        if not prod:
            continue
        try:
            res = fetch_battlenet_product(prod, reg)
            if res:
                out["battlenet"][prod] = res
        except Exception as e:
            print(f"[WARN] battlenet {prod}: {e}")

    for entry in tracked.get("gog", []):
        gid = str(entry.get("gameId") or "").strip()
        if not gid:
            continue
        try:
            res = fetch_gog_product(gid)
            if res:
                out["gog"][gid] = res
        except Exception as e:
            print(f"[WARN] gog {gid}: {e}")

    for entry in tracked.get("oracle", []):
        name = str(entry.get("name") or "").strip()
        if not name:
            continue
        key = norm_title(name)
        aid = str(entry.get("steamAppId") or "").strip()
        try:
            if not aid:
                aid = resolve_steam_appid(name) or ""
            if not aid:
                continue
            sinfo = out["steam"].get(aid) or fetch_steam_app(aid)
            if sinfo and "public" in sinfo.get("branches", {}):
                pub = sinfo["branches"]["public"]
                out["oracle"][key] = {
                    "steamAppId": aid,
                    "buildId": pub.get("buildId", ""),
                    "timeUpdated": pub.get("timeUpdated", 0),
                }
        except Exception as e:
            print(f"[WARN] oracle {name}: {e}")

    with open("updates-db.json", "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, sort_keys=True)
    print("Generated updates-db.json successfully.")


if __name__ == "__main__":
    main()