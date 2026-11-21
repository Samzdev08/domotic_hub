import anthropic
import json
import requests

client = anthropic.Anthropic(base_url="http://localhost:11434", api_key="ollama")

tools = [
    {
        "name": "api_climat",
        "description": (
            "Lit la mesure actuelle du capteur DHT11 du hub domotique : température en °C et humidité relative en %, "
            "toutes deux en nombres entiers, avec l'heure de la mesure. À utiliser pour toute question sur la "
            "température ou l'humidité actuelle de la pièce. Les valeurs changent dans le temps : rappelle cet outil "
            "à chaque nouvelle question, même si tu l'as déjà appelé avant. Si l'outil renvoie une erreur, lis le "
            "message d'erreur et explique la cause exacte à l'utilisateur (capteur en panne, hub injoignable, hub "
            "trop lent...). Ne donne jamais de valeur inventée ou estimée et réponds uniquement à ce qui est demandé : si on te demande la température, ne donne pas l'humidité, et inversement."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": []
        }
    }
]

messages = []

while True:
    input_text = input("Entrez votre question : ")
    if input_text.lower() in ("quit", "exit"):
        break

    messages.append({"role": "user", "content": input_text})

    response = client.messages.create(
        model = "qwen3:8b",
        max_tokens = 4000,
        tools = tools,
        tool_choice ={"type": "auto", "disable_parallel_tool_use": True},
        messages = messages,
    )

    if response.stop_reason == "tool_use":
        tool_use = next(block for block in response.content if block.type == "tool_use")

        est_erreur = False
        data = {}

        if tool_use.name == "api_climat":
            try:
                res = requests.get('http://127.0.0.1:8080/api/climat', timeout=5)

                if res.status_code != 200:
                    est_erreur = True
                    data = res.json()['erreur']
                else:
                    data = res.json()

            except requests.exceptions.ConnectionError:
                est_erreur = True
                data = {"erreur": "Impossible de joindre le hub (éteint ou hors réseau). Le capteur n'a pas pu être interrogé : son état est inconnu."}

            except requests.exceptions.Timeout:
                est_erreur = True
                data = {"erreur": "Le hub n'a pas répondu à temps."}

            except requests.exceptions.RequestException as e:
                est_erreur = True
                data = {"erreur": f"Erreur réseau inattendue : {e}"}
        else:
            est_erreur = True
            data = {"erreur": f"Outil inconnu : {tool_use.name}"}

        messages += [
            {"role": "assistant", "content": response.content},
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_use.id,
                        "content": json.dumps(data),
                        "is_error": est_erreur
                    }
                ]
            }
        ]

        response_final = client.messages.create(
            model = "qwen3:8b",
            max_tokens = 4000,
            tools = tools,
            messages = messages,
        )
    else:
        response_final = response

    messages.append({"role": "assistant", "content": response_final.content})

    for block in response_final.content:
        if block.type == "text":
            print(f"Assistant: {block.text}")
