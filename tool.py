import os
import requests


# 1. Fonction Météo
def get_weather(city: str) -> str:
  api_key = os.getenv("WEATHER_API_KEY")
  url = f"http://api.weatherapi.com/v1/current.json?key={api_key}&q={city}&lang=fr"
  try:
    res = requests.get(url).json()
    if "error" in res:
      return f"Impossible de trouver la météo pour {city}."
    temp = res["current"]["temp_c"]
    condition = res["current"]["condition"]["text"]
    return f"Météo actuelle à {city} : {temp}°C, {condition}."
  except Exception:
    return "Erreur lors de la récupération de la météo."


# 2. Fonction Recherche Internet (Tavily)
def search_internet(query: str) -> str:
  api_key = os.getenv("TAVILY_API_KEY")
  url = "https://api.tavily.com/search"
  try:
    res = requests.post(
        url, json={"api_key": api_key, "query": query, "max_results": 3}
    ).json()
    results = res.get("results", [])
    if not results:
      return "Aucun résultat trouvé."
    output = []
    for r in results:
      output.append(f"- [{r.get('title')}]({r.get('url')}): {r.get('content')}")
    return "\n".join(output)
  except Exception:
    return "Erreur lors de la recherche sur Internet."


# Dictionnaire global pour exécuter l'outil dynamiquement
AVAILABLE_TOOLS = {
    "get_weather": get_weather,
    "search_internet": search_internet,
}

# Schémas JSON pour que le LLM sache comment utiliser ces outils
tools_schema = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Récupère la météo actuelle pour une ville donnée.",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {
                        "type": "string",
                        "description": "Nom de la ville (ex: Paris, Lomé)",
                    }
                },
                "required": ["city"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_internet",
            "description": (
                "Effectue une recherche sur Internet pour des actualités ou"
                " des informations récentes."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Mots-clés de la recherche",
                    }
                },
                "required": ["query"],
            },
        },
    },
]