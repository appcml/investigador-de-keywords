"""
Investigador de Keywords - Backend FastAPI
APIs gratuitas: pytrends, SerpAPI (free tier), YouTube Data API, Wikipedia, Reddit, Claude AI
"""

from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import httpx
import asyncio
import os
import json
import re
from datetime import datetime, timedelta
from typing import Optional
import anthropic

app = FastAPI(title="Investigador de Keywords API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── Clientes ────────────────────────────────────────────────────────────────
claude_client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY", ""))
SERP_API_KEY   = os.getenv("SERP_API_KEY", "")        # https://serpapi.com  (100 búsquedas/mes gratis)
YT_API_KEY     = os.getenv("YOUTUBE_API_KEY", "")     # Google Cloud Console (gratis)
REDDIT_CLIENT  = os.getenv("REDDIT_CLIENT_ID", "")
REDDIT_SECRET  = os.getenv("REDDIT_SECRET", "")


# ═══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def country_to_hl(code: str) -> str:
    return {"CL":"es","MX":"es","AR":"es","ES":"es","CO":"es","VE":"es","US":"en"}.get(code.upper(),"es")

def country_to_gl(code: str) -> str:
    return code.lower()


# ═══════════════════════════════════════════════════════════════════════════════
# 1. GOOGLE TRENDS (pytrends - completamente gratis)
# ═══════════════════════════════════════════════════════════════════════════════

async def get_google_trends(keyword: str, country: str = "CL") -> dict:
    """Obtiene tendencias reales de Google Trends via pytrends"""
    try:
        from pytrends.request import TrendReq
        import pandas as pd

        def _sync_trends():
            pytrends = TrendReq(hl=country_to_hl(country), tz=360, timeout=(10,25))
            pytrends.build_payload(
                [keyword],
                cat=0,
                timeframe="today 3-m",
                geo=country.upper() if country.upper() != "US" else "",
                gprop=""
            )
            interest = pytrends.interest_over_time()
            related_q = pytrends.related_queries()
            related_t = pytrends.related_topics()
            suggestions = pytrends.suggestions(keyword=keyword)

            trend_data = []
            if not interest.empty and keyword in interest.columns:
                trend_data = interest[keyword].tolist()[-12:]

            top_queries = []
            if keyword in related_q and related_q[keyword].get("top") is not None:
                df = related_q[keyword]["top"].head(10)
                top_queries = df.to_dict("records")

            rising_queries = []
            if keyword in related_q and related_q[keyword].get("rising") is not None:
                df = related_q[keyword]["rising"].head(10)
                rising_queries = df.to_dict("records")

            return {
                "trend_12w": trend_data,
                "current_interest": trend_data[-1] if trend_data else 0,
                "peak_interest": max(trend_data) if trend_data else 0,
                "top_queries": top_queries,
                "rising_queries": rising_queries,
                "suggestions": suggestions[:8],
            }

        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, _sync_trends)
        return result
    except Exception as e:
        return {"error": str(e), "trend_12w": [], "top_queries": [], "rising_queries": [], "suggestions": []}


# ═══════════════════════════════════════════════════════════════════════════════
# 2. SERPAPI - Resultados reales de búsqueda (100 gratis/mes)
# ═══════════════════════════════════════════════════════════════════════════════

async def get_serp_data(keyword: str, country: str = "CL") -> dict:
    """Analiza los primeros resultados de Google para la keyword"""
    if not SERP_API_KEY:
        return {"error": "SERP_API_KEY no configurada", "results": []}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get("https://serpapi.com/search", params={
                "q": keyword,
                "api_key": SERP_API_KEY,
                "gl": country_to_gl(country),
                "hl": country_to_hl(country),
                "num": 10,
                "engine": "google"
            })
            data = r.json()

        results = []
        for item in data.get("organic_results", [])[:10]:
            results.append({
                "position": item.get("position"),
                "title": item.get("title"),
                "url": item.get("link"),
                "domain": item.get("displayed_link","").split("/")[0],
                "snippet": item.get("snippet","")[:200],
                "date": item.get("date"),
            })

        related_searches = [s.get("query","") for s in data.get("related_searches",[])]
        people_also_ask  = [q.get("question","") for q in data.get("related_questions",[])]
        ads_top          = len(data.get("ads",[]))
        knowledge_panel  = bool(data.get("knowledge_graph"))

        return {
            "results": results,
            "related_searches": related_searches,
            "people_also_ask": people_also_ask,
            "ads_count": ads_top,
            "has_knowledge_panel": knowledge_panel,
            "total_results_raw": data.get("search_information",{}).get("total_results",""),
        }
    except Exception as e:
        return {"error": str(e), "results": []}


# ═══════════════════════════════════════════════════════════════════════════════
# 3. YOUTUBE DATA API (gratis - 10.000 unidades/día)
# ═══════════════════════════════════════════════════════════════════════════════

async def get_youtube_data(keyword: str, country: str = "CL") -> dict:
    """Búsqueda en YouTube para medir demanda de video content"""
    if not YT_API_KEY:
        return {"error": "YOUTUBE_API_KEY no configurada", "videos": []}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get("https://www.googleapis.com/youtube/v3/search", params={
                "q": keyword,
                "key": YT_API_KEY,
                "part": "snippet",
                "type": "video",
                "regionCode": country.upper(),
                "relevanceLanguage": country_to_hl(country),
                "maxResults": 10,
                "order": "viewCount"
            })
            data = r.json()

        videos = []
        video_ids = []
        for item in data.get("items", []):
            vid_id = item["id"].get("videoId","")
            video_ids.append(vid_id)
            videos.append({
                "id": vid_id,
                "title": item["snippet"].get("title",""),
                "channel": item["snippet"].get("channelTitle",""),
                "published": item["snippet"].get("publishedAt","")[:10],
                "description": item["snippet"].get("description","")[:150],
            })

        # Obtener estadísticas de los videos
        if video_ids:
            r2 = await client.get("https://www.googleapis.com/youtube/v3/videos", params={
                "id": ",".join(video_ids),
                "key": YT_API_KEY,
                "part": "statistics"
            })
            stats_data = r2.json()
            for i, stat_item in enumerate(stats_data.get("items",[])):
                if i < len(videos):
                    s = stat_item.get("statistics",{})
                    videos[i]["views"]    = int(s.get("viewCount",0))
                    videos[i]["likes"]    = int(s.get("likeCount",0))
                    videos[i]["comments"] = int(s.get("commentCount",0))

        total_views = sum(v.get("views",0) for v in videos)
        return {
            "videos": videos[:8],
            "total_views_top10": total_views,
            "avg_views": round(total_views / len(videos)) if videos else 0,
        }
    except Exception as e:
        return {"error": str(e), "videos": []}


# ═══════════════════════════════════════════════════════════════════════════════
# 4. WIKIPEDIA API (100% gratis, sin key)
# ═══════════════════════════════════════════════════════════════════════════════

async def get_wikipedia_data(keyword: str, lang: str = "es") -> dict:
    """Contexto y popularidad desde Wikipedia"""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            # Buscar artículo
            r = await client.get(f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/{keyword.replace(' ','_')}")
            if r.status_code == 404:
                # Intentar búsqueda
                r2 = await client.get(f"https://{lang}.wikipedia.org/w/api.php", params={
                    "action":"opensearch","search":keyword,"limit":3,"format":"json"
                })
                suggestions = r2.json()
                if suggestions and len(suggestions) > 1 and suggestions[1]:
                    first = suggestions[1][0].replace(" ","_")
                    r = await client.get(f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/{first}")

            if r.status_code == 200:
                d = r.json()
                # Pageviews del último mes
                title = d.get("title","").replace(" ","_")
                end   = datetime.now().strftime("%Y%m%d")
                start = (datetime.now() - timedelta(days=30)).strftime("%Y%m%d")
                r3 = await client.get(
                    f"https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/es.wikipedia/all-access/all-agents/{title}/daily/{start}/{end}"
                )
                views_30d = 0
                if r3.status_code == 200:
                    items = r3.json().get("items",[])
                    views_30d = sum(i.get("views",0) for i in items)

                return {
                    "title": d.get("title",""),
                    "extract": d.get("extract","")[:400],
                    "page_url": d.get("content_urls",{}).get("desktop",{}).get("page",""),
                    "views_30d": views_30d,
                    "image": d.get("thumbnail",{}).get("source",""),
                }
        return {"title":"","extract":"","views_30d":0}
    except Exception as e:
        return {"error": str(e), "views_30d": 0}


# ═══════════════════════════════════════════════════════════════════════════════
# 5. REDDIT API (gratis con app registrada)
# ═══════════════════════════════════════════════════════════════════════════════

async def get_reddit_data(keyword: str) -> dict:
    """Busca discusiones en Reddit para medir sentimiento e interés"""
    try:
        headers = {"User-Agent": "KeywordResearcher/1.0"}
        async with httpx.AsyncClient(timeout=10, headers=headers) as client:
            r = await client.get("https://www.reddit.com/search.json", params={
                "q": keyword, "sort": "relevance", "limit": 10, "type": "link", "t": "month"
            })
            data = r.json()

        posts = []
        for child in data.get("data",{}).get("children",[])[:8]:
            p = child.get("data",{})
            posts.append({
                "title": p.get("title",""),
                "subreddit": p.get("subreddit",""),
                "score": p.get("score",0),
                "comments": p.get("num_comments",0),
                "url": f"https://reddit.com{p.get('permalink','')}",
                "created": datetime.fromtimestamp(p.get("created_utc",0)).strftime("%Y-%m-%d"),
            })

        total_engagement = sum(p["score"] + p["comments"] for p in posts)
        return {
            "posts": posts,
            "total_posts_found": data.get("data",{}).get("dist",0),
            "total_engagement": total_engagement,
        }
    except Exception as e:
        return {"error": str(e), "posts": []}


# ═══════════════════════════════════════════════════════════════════════════════
# 6. CLAUDE AI - Análisis inteligente y estimaciones
# ═══════════════════════════════════════════════════════════════════════════════

async def get_ai_analysis(keyword: str, country: str, real_data: dict) -> dict:
    """Claude analiza todos los datos reales y genera insights + estimaciones"""
    
    context = f"""Keyword: "{keyword}" | País: {country} | Fecha: {datetime.now().strftime('%B %Y')}

DATOS REALES RECOPILADOS:
- Google Trends: interés actual {real_data.get('trends',{}).get('current_interest',0)}/100, pico {real_data.get('trends',{}).get('peak_interest',0)}/100
- Queries en tendencia ascendente: {[q.get('query','') for q in real_data.get('trends',{}).get('rising_queries',[])[:5]]}
- Wikipedia vistas 30d: {real_data.get('wiki',{}).get('views_30d',0):,}
- YouTube vistas promedio top10: {real_data.get('youtube',{}).get('avg_views',0):,}
- Reddit engagement: {real_data.get('reddit',{}).get('total_engagement',0)}
- Resultados Google: {len(real_data.get('serp',{}).get('results',[]))} páginas analizadas
- People Also Ask: {real_data.get('serp',{}).get('people_also_ask',[])}
- Anuncios pagados detectados: {real_data.get('serp',{}).get('ads_count',0)}
"""

    sys_prompt = """Eres el motor de análisis SEO de una herramienta profesional. Devuelve SOLO JSON válido sin markdown.
Basándote en los datos reales proporcionados, genera estimaciones calibradas y análisis estratégico.
JSON requerido:
{
  "volume_estimate": int (búsquedas mensuales estimadas),
  "difficulty": int (0-100),
  "cpc_estimate": float (CPC en USD estimado basado en ads detectados),
  "competitive_density": float (0-1),
  "intent": "Informacional"|"Navegacional"|"Transaccional"|"Comercial",
  "seasonality": "Alta"|"Media"|"Baja",
  "trend_direction": "Subiendo"|"Estable"|"Bajando",
  "content_gap": string (oportunidad de contenido detectada),
  "best_content_type": string (tipo de contenido recomendado),
  "long_tail_keywords": [8 keywords de cola larga específicas],
  "lsi_keywords": [6 términos semánticamente relacionados],
  "questions_to_answer": [5 preguntas que la gente hace sobre este tema],
  "monetization_potential": "Alto"|"Medio"|"Bajo",
  "editorial_angle": string (mejor ángulo para un sitio de noticias en Chile),
  "best_posting_time": string (cuándo publicar),
  "competitor_weaknesses": string (debilidades detectadas en los resultados),
  "seo_score_potential": int (potencial de posicionamiento 0-100),
  "summary": string (análisis ejecutivo de 3 oraciones)
}"""

    try:
        msg = claude_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1500,
            system=sys_prompt,
            messages=[{"role":"user","content":context}]
        )
        text = msg.content[0].text
        # limpiar posibles backticks
        text = re.sub(r"```json|```","",text).strip()
        return json.loads(text)
    except Exception as e:
        return {"error": str(e)}


# ═══════════════════════════════════════════════════════════════════════════════
# 7. AUTOCOMPLETE (Google Suggest - gratis, sin key)
# ═══════════════════════════════════════════════════════════════════════════════

async def get_autocomplete(keyword: str, country: str = "CL") -> dict:
    """Google Autocomplete y variaciones alfabéticas"""
    suggestions = set()
    modifiers = ["", "qué es", "cómo", "por qué", "cuándo", "mejor", "vs", "precio", "2024", "2025", "2026", "gratis", "sin"]

    async with httpx.AsyncClient(timeout=10) as client:
        tasks = []
        for mod in modifiers[:8]:
            q = f"{mod} {keyword}" if mod else keyword
            tasks.append(client.get(
                "https://suggestqueries.google.com/complete/search",
                params={"q":q,"client":"firefox","hl":country_to_hl(country),"gl":country_to_gl(country)}
            ))
        responses = await asyncio.gather(*tasks, return_exceptions=True)

    for resp in responses:
        if isinstance(resp, Exception):
            continue
        try:
            data = resp.json()
            for s in data[1]:
                suggestions.add(s)
        except:
            pass

    # Variaciones alfabéticas (a-z)
    alpha_suggestions = []
    async with httpx.AsyncClient(timeout=8) as client:
        alpha_tasks = []
        for letter in "abcdefghijklmnopqrstuvwxyz":
            alpha_tasks.append(client.get(
                "https://suggestqueries.google.com/complete/search",
                params={"q":f"{keyword} {letter}","client":"firefox","hl":country_to_hl(country)}
            ))
        alpha_responses = await asyncio.gather(*alpha_tasks, return_exceptions=True)

    for resp in alpha_responses:
        if isinstance(resp, Exception):
            continue
        try:
            data = resp.json()
            alpha_suggestions.extend(data[1])
        except:
            pass

    return {
        "modifier_suggestions": sorted(list(suggestions))[:30],
        "alpha_suggestions": list(set(alpha_suggestions))[:50],
        "total": len(suggestions) + len(set(alpha_suggestions))
    }


# ═══════════════════════════════════════════════════════════════════════════════
# ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/")
async def root():
    return {"status":"ok","version":"1.0.0","tool":"Investigador de Keywords"}


@app.get("/api/analyze")
async def analyze_keyword(
    q: str = Query(..., description="Keyword a analizar"),
    country: str = Query("CL", description="Código de país"),
    modules: str = Query("trends,serp,youtube,wiki,reddit,ai,autocomplete", description="Módulos a ejecutar separados por coma")
):
    """Análisis completo de una keyword usando todas las fuentes disponibles"""
    mods = set(modules.split(","))
    tasks = {}

    if "trends"      in mods: tasks["trends"]      = get_google_trends(q, country)
    if "serp"        in mods: tasks["serp"]        = get_serp_data(q, country)
    if "youtube"     in mods: tasks["youtube"]     = get_youtube_data(q, country)
    if "wiki"        in mods: tasks["wiki"]        = get_wikipedia_data(q, country_to_hl(country))
    if "reddit"      in mods: tasks["reddit"]      = get_reddit_data(q)
    if "autocomplete"in mods: tasks["autocomplete"]= get_autocomplete(q, country)

    results = {}
    if tasks:
        gathered = await asyncio.gather(*tasks.values(), return_exceptions=True)
        for key, val in zip(tasks.keys(), gathered):
            results[key] = val if not isinstance(val, Exception) else {"error": str(val)}

    if "ai" in mods:
        results["ai"] = await get_ai_analysis(q, country, results)

    return {
        "keyword": q,
        "country": country,
        "timestamp": datetime.now().isoformat(),
        "data": results
    }


@app.get("/api/trends")
async def trends_only(q: str, country: str = "CL"):
    return await get_google_trends(q, country)


@app.get("/api/autocomplete")
async def autocomplete_only(q: str, country: str = "CL"):
    return await get_autocomplete(q, country)


@app.get("/api/compare")
async def compare_keywords(
    a: str = Query(...),
    b: str = Query(...),
    country: str = Query("CL")
):
    """Compara dos keywords en paralelo"""
    res_a, res_b = await asyncio.gather(
        analyze_keyword(a, country, "trends,wiki,reddit,ai"),
        analyze_keyword(b, country, "trends,wiki,reddit,ai")
    )
    return {"keyword_a": res_a, "keyword_b": res_b, "country": country}


@app.get("/api/bulk")
async def bulk_analyze(
    keywords: str = Query(..., description="Keywords separadas por coma"),
    country: str = Query("CL"),
    modules: str = Query("trends,autocomplete,ai")
):
    """Análisis de múltiples keywords a la vez"""
    kw_list = [k.strip() for k in keywords.split(",") if k.strip()][:10]
    tasks   = [analyze_keyword(kw, country, modules) for kw in kw_list]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    return {
        "keywords": kw_list,
        "results": [r if not isinstance(r, Exception) else {"error":str(r)} for r in results]
    }


@app.get("/api/youtube")
async def youtube_only(q: str, country: str = "CL"):
    return await get_youtube_data(q, country)


@app.get("/api/serp")
async def serp_only(q: str, country: str = "CL"):
    return await get_serp_data(q, country)
