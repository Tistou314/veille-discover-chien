#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Veille Discover - niche chien (chien.pagesjaunes.fr)
=====================================================

Tous les matins :
    python3 veille_discover_chien.py

Le script :
  1. Lit sources_chien.json
  2. Decouvre automatiquement le flux RSS de chaque source (ou utilise celui fourni)
  3. Recupere tous les articles recents
  4. Filtre la thematique chien, deduplique, detecte les sujets "chauds" (repris par
     plusieurs sources)
  5. Score le potentiel Discover de chaque sujet
  6. Ecrit un tableau de bord HTML triable : digest/digest_AAAA-MM-JJ.html
  7. Tient un historique (historique.json) pour ne plus remonter les sujets deja vus

Options :
    --demo        utilise des flux locaux de demonstration (aucun reseau requis)
    --jours N     fenetre de fraicheur en jours (defaut 4)
    --no-history  ne lit/ecrit pas l'historique (tout est considere comme neuf)

Dependance : pip install feedparser
"""

import argparse
import datetime as dt
import html
import json
import os
import re
import sys
import unicodedata
import urllib.request
from email.utils import parsedate_to_datetime

import feedparser

ICI = os.path.dirname(os.path.abspath(__file__))
FICHIER_SOURCES = os.path.join(ICI, "sources_chien.json")
FICHIER_HISTO = os.path.join(ICI, "historique.json")
DOSSIER_DIGEST = os.path.join(ICI, "digest")
UA = "VeilleDiscoverChien/1.0 (+veille editoriale)"

# --------------------------------------------------------------------------- #
#  Signaux Discover (titres)                                                   #
# --------------------------------------------------------------------------- #
# Mots a fort potentiel emotionnel / curiosite, observes sur Wamiz, Woopets,
# Le Mag du Chien. Ponderation indicative.
MOTS_DISCOVER = {
    # emotion / sauvetage (tres fort en Discover)
    "sauve": 3, "sauvetage": 3, "secouru": 3, "abandonne": 3, "abandon": 3,
    "refuge": 2, "adopte": 2, "adoption": 2, "miracle": 3, "incroyable": 2,
    "emouvant": 3, "bouleversant": 3, "pleure": 3, "larmes": 3, "touchant": 2,
    "retrouve": 2, "disparu": 2, "fugue": 2, "heros": 2, "fidele": 1,
    "refuse": 2, "surprise": 2, "inattendu": 2, "transformation": 2,
    "piege": 2, "coince": 2, "bloque": 2, "deuil": 2, "reaction": 2,
    # danger / sante / securite (utiles, angle pratique)
    "danger": 2, "mourir": 2, "mort": 1, "alerte": 2, "attention": 1,
    "rappel": 2, "toxique": 2, "canicule": 2, "voiture": 1, "interdit": 2,
    "risque": 1, "symptome": 1, "vaccin": 1, "maladie": 1,
    # science / etude (bon pour evergreen Discover)
    "etude": 2, "science": 2, "revele": 2, "decouverte": 2, "chercheurs": 1,
    "pourquoi": 2, "comment": 1, "raison": 1,
    # format video/photo (booste le CTR Discover)
    "video": 1, "photos": 1, "images": 1,
}

# Marqueurs thematiques chien (au moins un doit apparaitre pour garder l'item,
# sauf sujets transversaux ci-dessous).
MOTS_CHIEN = [
    "chien", "chiot", "canin", "canine", "toutou", "chienne", "aboi",
    "berger", "labrador", "bouledogue", "golden", "husky", "beagle",
    "chihuahua", "border collie", "malinois", "cocker", "carlin",
    "croquettes", "dressage", "education canine",
]
# Sujets transversaux chien+chat a conserver meme sans le mot "chien".
MOTS_TRANSVERSAUX = ["canicule", "voiture", "refuge", "spa", "maltraitance",
                     "abandon", "adoption"]

# Mots trop frequents dans la niche : ignores SEULEMENT pour comparer deux sujets
# entre eux (pas pour le filtrage thematique), sinon tout se ressemble.
MOTS_NICHE_IGNORES = {"chien", "chiens", "chiot", "chiots", "chienne", "chiennes",
                      "chat", "chats", "animal", "animaux", "maitre", "maitres"}

STOPWORDS = set("""
au aux avec ce ces dans de des du elle en et eux il je la le les leur lui ma
mais me meme mes moi mon ne nos notre nous on ou par pas pour qu que qui sa se
ses son sur ta te tes toi ton tu un une vos votre vous c d j l m n s t y a son
ses cette cet ceux quoi dont est sont ete a ans apres avant chez tout toute
tous toutes plus moins tres deja encore alors quand comme sans sous entre
""".split())


# --------------------------------------------------------------------------- #
#  Utilitaires texte                                                           #
# --------------------------------------------------------------------------- #
def sans_accents(txt: str) -> str:
    txt = unicodedata.normalize("NFD", txt)
    return "".join(c for c in txt if unicodedata.category(c) != "Mn")


def normalise(txt: str) -> str:
    """minuscule, sans accents, sans ponctuation -> pour comparaison."""
    txt = sans_accents((txt or "").lower())
    txt = re.sub(r"[^a-z0-9\s]", " ", txt)
    return re.sub(r"\s+", " ", txt).strip()


def tokens_signifiants(txt: str, pour_comparaison=False) -> set:
    base = {m for m in normalise(txt).split() if m not in STOPWORDS and len(m) > 2}
    if pour_comparaison:
        base -= MOTS_NICHE_IGNORES
    return base


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


# --------------------------------------------------------------------------- #
#  Decouverte de flux RSS                                                      #
# --------------------------------------------------------------------------- #
CHEMINS_FLUX = ["/feed/", "/feed", "/rss", "/rss.xml", "/feed/rss/",
                "/flux-rss", "/?feed=rss2", "/feed/atom/"]


def http_get(url: str, timeout=15) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def racine(url: str) -> str:
    m = re.match(r"(https?://[^/]+)", url)
    return m.group(1) if m else url


def decouvrir_flux(home: str) -> str | None:
    """Trouve l'URL d'un flux RSS/Atom a partir d'une page."""
    # 1) balise <link rel="alternate" type="application/rss+xml" href="...">
    try:
        page = http_get(home).decode("utf-8", "ignore")
        for m in re.finditer(r"<link[^>]+>", page, re.I):
            bloc = m.group(0)
            if "alternate" in bloc.lower() and ("rss" in bloc.lower() or "atom" in bloc.lower()):
                href = re.search(r'href=["\']([^"\']+)["\']', bloc, re.I)
                if href:
                    lien = href.group(1)
                    if lien.startswith("/"):
                        lien = racine(home) + lien
                    return lien
    except Exception:
        pass
    # 2) chemins classiques
    for chemin in CHEMINS_FLUX:
        for base in (home.rstrip("/"), racine(home)):
            cand = base + chemin
            try:
                d = feedparser.parse(http_get(cand))
                if d.entries:
                    return cand
            except Exception:
                continue
    return None


# --------------------------------------------------------------------------- #
#  Recuperation et scoring                                                     #
# --------------------------------------------------------------------------- #
def date_entree(e) -> dt.datetime | None:
    for champ in ("published", "updated"):
        val = e.get(champ)
        if val:
            try:
                d = parsedate_to_datetime(val)
                return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)
            except Exception:
                pass
    if e.get("published_parsed"):
        return dt.datetime(*e.published_parsed[:6], tzinfo=dt.timezone.utc)
    return None


MOTS_CHAT = ["chat", "chats", "chaton", "chatons", "chatte", "chattes",
             "felin", "feline", "felins", "matou", "matous"]

MOTS_EXCLUS = ["robot", "horoscope", "astrologie", "signe chinois",
               "sauce chien", "jeu video", "playstation", "nintendo"]


def concerne_chien(titre: str, resume: str) -> bool:
    t = normalise(titre + " " + resume)
    if any(re.search(rf"\b{m}", t) for m in MOTS_EXCLUS):
        return False
    if any(re.search(rf"\b{m}", t) for m in MOTS_CHIEN):
        return True
    if any(re.search(rf"\b{m}", t) for m in MOTS_CHAT):
        return False          # sujet chat (sans chien) -> exclu
    if any(re.search(rf"\b{m}", t) for m in MOTS_TRANSVERSAUX):
        return True
    return False


def score_titre(titre: str) -> tuple[int, list]:
    """Score les signaux Discover presents dans le titre."""
    t = normalise(titre)
    score, raisons = 0, []
    for mot, poids in MOTS_DISCOVER.items():
        if re.search(rf"\b{mot}\w*", t):
            score += poids
            raisons.append(mot)
    if re.search(r"\d", titre):                     # un chiffre accroche
        score += 1; raisons.append("chiffre")
    if "?" in titre:                                # question
        score += 1; raisons.append("question")
    if re.search(r"\b(cette?|ce)\b.*\bqui\b", t):   # tournure "ce X qui..."
        score += 1; raisons.append("tournure-recit")
    nb = len(titre.split())
    if 8 <= nb <= 16:                               # longueur ideale feed
        score += 1; raisons.append("longueur-ok")
    return score, raisons


def score_fraicheur(date: dt.datetime | None, maintenant: dt.datetime) -> int:
    if not date:
        return 0
    h = (maintenant - date).total_seconds() / 3600
    if h <= 12:  return 5
    if h <= 24:  return 4
    if h <= 48:  return 3
    if h <= 72:  return 1
    return 0


# --------------------------------------------------------------------------- #
#  Pipeline                                                                    #
# --------------------------------------------------------------------------- #
def charger_sources():
    with open(FICHIER_SOURCES, encoding="utf-8") as f:
        return [s for s in json.load(f)["sources"] if s.get("actif", True)]


def charger_historique(actif: bool) -> set:
    if not actif or not os.path.exists(FICHIER_HISTO):
        return set()
    with open(FICHIER_HISTO, encoding="utf-8") as f:
        return set(json.load(f).get("vus", []))


def sauver_historique(vus: set, actif: bool):
    if not actif:
        return
    with open(FICHIER_HISTO, "w", encoding="utf-8") as f:
        json.dump({"vus": sorted(vus),
                   "maj": dt.datetime.now().isoformat(timespec="seconds")},
                  f, ensure_ascii=False, indent=2)


def titre_depuis_url(url: str) -> str:
    """Derive un titre lisible depuis le slug d'une URL (fallback sitemap)."""
    slug = re.sub(r"[?#].*$", "", url).rstrip("/").split("/")[-1]
    slug = re.sub(r"\.\w+$", "", slug)            # retire extension eventuelle
    slug = re.sub(r"[-_]+", " ", slug).strip()
    slug = re.sub(r"\b\d{4,}\b", "", slug).strip()  # retire id numeriques longs
    return slug[:1].upper() + slug[1:] if slug else ""


CHEMINS_SITEMAP = ["/sitemap-news.xml", "/news-sitemap.xml", "/sitemap_news.xml",
                   "/sitemap-actualites.xml", "/sitemap-posts.xml", "/sitemap.xml"]


def entrees_sitemap(home: str, maintenant: dt.datetime):
    """Repli quand un site n'a pas de RSS : lit un sitemap news et derive des entrees.
    Le titre vient du slug d'URL (souvent = titre Discover), exploitable pour le score."""
    for chemin in CHEMINS_SITEMAP:
        for base in (racine(home), home.rstrip("/")):
            cand = base + chemin
            try:
                xml = http_get(cand).decode("utf-8", "ignore")
            except Exception:
                continue
            blocs = re.findall(r"<url>(.*?)</url>", xml, re.S)
            entrees = []
            for b in blocs:
                loc = re.search(r"<loc>\s*([^<]+?)\s*</loc>", b)
                if not loc:
                    continue
                lien = loc.group(1).strip()
                dm = re.search(r"<(?:news:)?publication_date>\s*([^<]+)", b) \
                     or re.search(r"<lastmod>\s*([^<]+)", b)
                date = None
                if dm:
                    try:
                        date = dt.datetime.fromisoformat(dm.group(1).strip().replace("Z", "+00:00"))
                        if not date.tzinfo:
                            date = date.replace(tzinfo=dt.timezone.utc)
                    except Exception:
                        date = None
                tm = re.search(r"<news:title>\s*([^<]+)", b)
                titre = tm.group(1).strip() if tm else titre_depuis_url(lien)
                entrees.append({"title": titre, "link": lien,
                                "summary": "", "date": date})
            if entrees:
                return entrees, cand
    return [], None


def entrees_flux(url: str):
    """Entrees normalisees depuis un flux RSS/Atom."""
    data = feedparser.parse(http_get(url))
    out = []
    for e in data.entries:
        out.append({"title": (e.get("title") or "").strip(),
                    "link": (e.get("link") or "").strip(),
                    "summary": re.sub(r"<[^>]+>", " ", e.get("summary", ""))[:400],
                    "date": date_entree(e)})
    return out


def resoudre_source(s, maintenant, demo_dir=None):
    """Renvoie (statut, type, url, entrees) pour une source.
    type ∈ {rss, sitemap}. statut ∈ {ok, vide, introuvable, erreur}."""
    if demo_dir:
        demo_map = {"Wamiz - Actu chien": "wamiz", "Woopets - Actu chien": "woopets",
                    "Le Mag du Chien (Ouest-France)": "le"}
        slug = demo_map.get(s["nom"])
        chemin = os.path.join(demo_dir, slug + ".xml") if slug else None
        if not (chemin and os.path.exists(chemin)):
            return "introuvable", None, None, []
        d = feedparser.parse(chemin)
        ent = [{"title": e.get("title", ""), "link": e.get("link", ""),
                "summary": re.sub(r"<[^>]+>", " ", e.get("summary", ""))[:400],
                "date": date_entree(e)} for e in d.entries]
        return ("ok" if ent else "vide"), "rss", chemin, ent
    # 1) flux RSS (fourni ou auto-decouvert)
    url = s.get("flux") or decouvrir_flux(s["home"])
    if url:
        try:
            ent = entrees_flux(url)
            if ent:
                return "ok", "rss", url, ent
        except Exception:
            pass
    # 2) repli sitemap news
    try:
        ent, url_sm = entrees_sitemap(s["home"], maintenant)
        if ent:
            return "ok", "sitemap", url_sm, ent
    except Exception:
        return "erreur", None, None, []
    return "introuvable", None, None, []


def collecter(sources, jours, demo_dir=None):
    items, maintenant = [], dt.datetime.now(dt.timezone.utc)
    limite = maintenant - dt.timedelta(days=jours)
    for s in sources:
        statut, typ, url, entrees = resoudre_source(s, maintenant, demo_dir)
        if statut != "ok":
            print(f"  [!] {statut} : {s['nom']}" + (f" ({s['home']})" if not demo_dir else ""))
            continue
        gardes = 0
        for e in entrees:
            titre = (e["title"] or "").strip()
            lien = (e["link"] or "").strip()
            resume = (e["summary"] or "").strip()
            if not titre or not lien:
                continue
            d = e["date"]
            if typ == "sitemap" and not d:
                continue
            if d and d < limite:
                continue
            if not concerne_chien(titre, resume):
                continue
            st, raisons = score_titre(titre)
            if typ == "sitemap":
                raisons = raisons + ["via-sitemap"]
            items.append({
                "titre": titre, "lien": lien, "resume": resume,
                "date": d, "source": s["nom"], "priorite": s.get("priorite", 1),
                "score_titre": st, "raisons": raisons,
                "score_fraicheur": score_fraicheur(d, maintenant),
                "tokens": tokens_signifiants(titre + " " + resume, pour_comparaison=True),
            })
            gardes += 1
        tag = "" if typ == "rss" else " [sitemap]"
        print(f"  [ok]{tag} {s['nom']} : {gardes} sujet(s) chien")
    return items, maintenant


def canon_lien(url: str) -> str:
    """Normalise une URL pour reperer les doublons exacts (params de tracking, slash final)."""
    url = re.sub(r"[?#].*$", "", url.strip())          # retire query + fragment
    url = re.sub(r"/+$", "", url)                       # retire slash(s) final(aux)
    return url.lower()


def dedup_liens(items):
    """Fusionne les items partageant la meme URL (meme article sur 2 rubriques)."""
    par_lien = {}
    for it in items:
        cle = canon_lien(it["lien"])
        if cle in par_lien:
            par_lien[cle]["_sources"].add(it["source"])
        else:
            it["_sources"] = {it["source"]}
            par_lien[cle] = it
    return list(par_lien.values())


def regrouper_sujets(items, seuil=0.42, communs_min=3):
    """Regroupe les items traitant du meme sujet (reformule par plusieurs sources).
    Critere : Jaccard >= seuil OU au moins `communs_min` tokens significatifs partages."""
    groupes = []
    for it in sorted(items, key=lambda x: x["date"] or dt.datetime.min.replace(tzinfo=dt.timezone.utc), reverse=True):
        place = False
        for g in groupes:
            communs = len(it["tokens"] & g["tokens"])
            if jaccard(it["tokens"], g["tokens"]) >= seuil or communs >= communs_min:
                g["membres"].append(it)
                g["sources"] |= it.get("_sources", {it["source"]})
                g["tokens"] |= it["tokens"]
                place = True
                break
        if not place:
            groupes.append({"membres": [it],
                            "sources": set(it.get("_sources", {it["source"]})),
                            "tokens": set(it["tokens"])})
    return groupes


def finaliser(groupes, deja_vus):
    """Choisit l'item representant de chaque groupe et calcule le score final."""
    sujets = []
    for g in groupes:
        # representant = l'item le mieux score (titre + fraicheur + priorite source)
        rep = max(g["membres"], key=lambda x: x["score_titre"] + x["score_fraicheur"] + x["priorite"])
        nb_sources = len(g["sources"])
        deja = rep["lien"] in deja_vus
        score = (rep["score_titre"]
                 + rep["score_fraicheur"]
                 + rep["priorite"]
                 + (nb_sources - 1) * 3)        # bonus "sujet chaud" repris ailleurs
        if deja:
            score -= 6                          # on degrade ce qu'on a deja vu
        sujets.append({
            **rep,
            "nb_sources": nb_sources,
            "autres_sources": sorted(g["sources"] - {rep["source"]}),
            "deja_vu": deja,
            "chaud": nb_sources >= 2,
            "score_final": score,
        })
    sujets.sort(key=lambda x: x["score_final"], reverse=True)
    return sujets


# --------------------------------------------------------------------------- #
#  Rendu HTML                                                                  #
# --------------------------------------------------------------------------- #
def rendre_html(sujets, date_jour):
    lignes = []
    for i, s in enumerate(sujets):
        ds = s["date"].astimezone().strftime("%d/%m %H:%M") if s["date"] else "?"
        badges = []
        if s["chaud"]:
            badges.append(f'<span class="b chaud">chaud · {s["nb_sources"]} sources</span>')
        if s["deja_vu"]:
            badges.append('<span class="b vu">deja vu</span>')
        for r in s["raisons"][:4]:
            badges.append(f'<span class="b sig">{html.escape(r)}</span>')
        autres = ""
        if s["autres_sources"]:
            autres = " · aussi : " + html.escape(", ".join(s["autres_sources"]))
        lignes.append(f"""
        <tr data-score="{s['score_final']}" data-date="{(s['date'].timestamp() if s['date'] else 0)}"
            data-chaud="{int(s['chaud'])}" data-vu="{int(s['deja_vu'])}"
            class="{'vu' if s['deja_vu'] else ''}">
          <td class="rang">{i+1}</td>
          <td class="sc">{s['score_final']}</td>
          <td class="titre">
            <a href="{html.escape(s['lien'])}" target="_blank" rel="noopener">{html.escape(s['titre'])}</a>
            <div class="meta">{html.escape(s['source'])}{autres} · {ds}</div>
            <div class="badges">{''.join(badges)}</div>
          </td>
        </tr>""")
    nb_chaud = sum(1 for s in sujets if s["chaud"])
    nb_neuf = sum(1 for s in sujets if not s["deja_vu"])
    return f"""<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Veille Discover chien · {date_jour}</title>
<style>
 :root{{--bg:#0f1d2f;--card:#16263c;--line:#243a56;--txt:#e8eef6;--mut:#90a4c0;
        --accent:#4f9cff;--chaud:#ff6b4a;--vu:#5a6b82}}
 *{{box-sizing:border-box}} body{{margin:0;background:#0f1d2f;color:#e8eef6;
   font:15px/1.5 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif}}
 .wrap{{max-width:880px;margin:0 auto;padding:24px 16px 64px}}
 h1{{font-size:20px;margin:0 0 4px}} .sub{{color:#90a4c0;font-size:13px;margin-bottom:18px}}
 .bar{{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:16px}}
 button{{background:#16263c;color:#e8eef6;border:1px solid #243a56;border-radius:8px;
   padding:7px 12px;font-size:13px;cursor:pointer}}
 button.on{{background:#4f9cff;border-color:#4f9cff;color:#06121f;font-weight:600}}
 input{{flex:1;min-width:140px;background:#16263c;color:#e8eef6;border:1px solid #243a56;
   border-radius:8px;padding:7px 12px;font-size:13px}}
 table{{width:100%;border-collapse:collapse}}
 td{{border-top:1px solid #243a56;padding:12px 8px;vertical-align:top}}
 tr.vu{{opacity:.5}} .rang{{color:#90a4c0;width:28px;text-align:right}}
 .sc{{width:34px;text-align:center;font-weight:700;color:#4f9cff}}
 .titre a{{color:#e8eef6;text-decoration:none;font-weight:600}}
 .titre a:hover{{color:#4f9cff}}
 .meta{{color:#90a4c0;font-size:12px;margin-top:3px}}
 .badges{{margin-top:6px;display:flex;gap:5px;flex-wrap:wrap}}
 .b{{font-size:11px;padding:2px 7px;border-radius:99px;border:1px solid #243a56;color:#90a4c0}}
 .b.chaud{{background:#3a1c14;border-color:#ff6b4a;color:#ff8e74}}
 .b.vu{{color:#5a6b82}} .b.sig{{background:#11243d}}
</style></head><body><div class="wrap">
 <h1>Veille Discover · niche chien</h1>
 <div class="sub">{date_jour} · {len(sujets)} sujets · {nb_neuf} neufs · {nb_chaud} chauds (multi-sources)</div>
 <div class="bar">
   <button class="on" data-f="tous">Tous</button>
   <button data-f="neuf">Neufs seulement</button>
   <button data-f="chaud">Chauds</button>
   <button id="tri">Trier : score ▾</button>
   <input id="q" placeholder="filtrer un mot-cle...">
 </div>
 <table><tbody id="t">{''.join(lignes)}</tbody></table>
</div>
<script>
 const tb=document.getElementById('t'), q=document.getElementById('q');
 let filtre='tous', triDate=false;
 function maj(){{
   [...tb.rows].forEach(r=>{{
     const okF = filtre==='tous' || (filtre==='neuf'&&r.dataset.vu==='0')
                 || (filtre==='chaud'&&r.dataset.chaud==='1');
     const okQ = r.textContent.toLowerCase().includes(q.value.toLowerCase());
     r.style.display = (okF&&okQ)?'':'none';
   }});
 }}
 document.querySelectorAll('[data-f]').forEach(b=>b.onclick=()=>{{
   document.querySelectorAll('[data-f]').forEach(x=>x.classList.remove('on'));
   b.classList.add('on'); filtre=b.dataset.f; maj();
 }});
 q.oninput=maj;
 document.getElementById('tri').onclick=function(){{
   triDate=!triDate; this.textContent='Trier : '+(triDate?'date ▾':'score ▾');
   const rows=[...tb.rows].sort((a,b)=>
     triDate ? b.dataset.date-a.dataset.date : b.dataset.score-a.dataset.score);
   rows.forEach(r=>tb.appendChild(r));
 }};
</script></body></html>"""


# --------------------------------------------------------------------------- #
#  Main                                                                        #
# --------------------------------------------------------------------------- #
def diagnostiquer(sources, demo_dir=None):
    """Mode --check : teste chaque source et rapporte l'etat, sans generer de digest."""
    maintenant = dt.datetime.now(dt.timezone.utc)
    ok = rss = sm = ko = 0
    print(f"Diagnostic des sources ({len(sources)} actives)\n" + "-" * 60)
    for s in sources:
        statut, typ, url, entrees = resoudre_source(s, maintenant, demo_dir)
        if statut == "ok":
            ok += 1
            rss += (typ == "rss"); sm += (typ == "sitemap")
            print(f"  OK   [{typ:7}] {s['nom']:38} {len(entrees):>3} items")
            print(f"                 -> {url}")
        else:
            ko += 1
            print(f"  {statut.upper():4} [-      ] {s['nom']:38}  ({s['home']})")
    print("-" * 60)
    print(f"{ok} OK ({rss} RSS, {sm} via sitemap) · {ko} a corriger.")
    if ko:
        print("Pour une source KO : trouve son flux et colle-le dans le champ "
              "'flux' de sources_chien.json (ou mets actif=false).")


def main():
    ap = argparse.ArgumentParser(description="Veille Discover niche chien")
    ap.add_argument("--demo", action="store_true", help="flux locaux (sans reseau)")
    ap.add_argument("--jours", type=int, default=4, help="fenetre de fraicheur (jours)")
    ap.add_argument("--no-history", action="store_true", help="ignorer l'historique")
    ap.add_argument("--sortie", default=DOSSIER_DIGEST,
                    help="dossier de sortie du digest (ex: ~/public_html/veille)")
    ap.add_argument("--check", action="store_true",
                    help="diagnostic : teste chaque source et rapporte son etat")
    args = ap.parse_args()

    sources = charger_sources()
    demo_dir = os.path.join(ICI, "demo_flux") if args.demo else None
    if args.check:
        diagnostiquer(sources, demo_dir)
        return

    dossier_sortie = os.path.expanduser(args.sortie)
    os.makedirs(dossier_sortie, exist_ok=True)
    histo_actif = not args.no_history
    deja_vus = charger_historique(histo_actif)

    print(f"Veille Discover chien · {len(sources)} sources · fenetre {args.jours} j")
    items, _ = collecter(sources, args.jours, demo_dir)
    items = dedup_liens(items)
    print(f"{len(items)} articles chien dans la fenetre (apres dedup URL).")

    groupes = regrouper_sujets(items)
    sujets = finaliser(groupes, deja_vus)
    print(f"{len(sujets)} sujets distincts apres regroupement.")

    date_jour = dt.date.today().isoformat()
    page = rendre_html(sujets, date_jour)
    sortie = os.path.join(dossier_sortie, f"digest_{date_jour}.html")
    with open(sortie, "w", encoding="utf-8") as f:
        f.write(page)
    # index.html = toujours le dernier digest -> un seul lien a mettre en favori
    with open(os.path.join(dossier_sortie, "index.html"), "w", encoding="utf-8") as f:
        f.write(page)
    print(f"\nTableau de bord : {sortie}")
    print(f"Dernier digest   : {os.path.join(dossier_sortie, 'index.html')}")

    # on memorise tout ce qui a ete affiche pour ne plus le remonter demain
    for s in sujets:
        deja_vus.add(s["lien"])
    sauver_historique(deja_vus, histo_actif)

    if sujets[:5]:
        print("\nTop 5 du jour :")
        for i, s in enumerate(sujets[:5], 1):
            tag = " [CHAUD]" if s["chaud"] else ""
            print(f"  {i}. ({s['score_final']}){tag} {s['titre'][:70]}")


if __name__ == "__main__":
    main()
