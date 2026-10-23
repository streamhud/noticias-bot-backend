import time
import os
import re
import asyncio
import json
import feedparser
import requests
import edge_tts
from dotenv import load_dotenv
import google.generativeai as genai
from supabase import create_client, Client

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

genai.configure(api_key=GEMINI_API_KEY)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Lista extendida de fuentes de noticias
FEEDS = [
    # Locales y Regionales (Chubut / Patagonia)
    "https://radio3cadenapatagonia.com.ar/feed/",
    "https://www.elchubut.com.ar/rss",
    "https://www.diariojornada.com.ar/rss/",
    "https://www.adnsur.com.ar/rss/feed.xml",
    "https://www.red43.com.ar/rss",
    "https://purapasion.com.ar/feed/",
    
    # Nacionales y Agencias Principales
    "https://news.google.com/rss?hl=es-419&gl=AR&ceid=AR:es-419", # Google News Argentina
    "https://www.infobae.com/feed/", # Infobae general
    "https://tn.com.ar/feed/", # TN Todo Noticias
    
    # Alertas Especiales (Catástrofes, Atentados, Fúnebres) escaneadas en tiempo real
    "https://news.google.com/rss/search?q=catástrofe+OR+sismo+OR+atentado+OR+falleció+OR+muerte+OR+alerta+urgente&hl=es-419&gl=AR&ceid=AR:es-419"
]

VOZ_ARGENTINA = "es-AR-ElenaNeural"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

def obtener_metadatos_html(url):
    try:
        respuesta = requests.get(url, headers=HEADERS, timeout=10)
        html = respuesta.text
        
        img_match = re.search(r'<meta\s+(?:property|name)="og:image"\s+content="([^"]+)"', html, re.IGNORECASE)
        imagen_url = img_match.group(1) if img_match else None
        
        yt_match = re.search(r'(?:youtube\.com\/(?:embed\/|watch\?v=)|youtu\.be\/)([a-zA-Z0-9_-]{11})', html)
        media_embed = None
        if yt_match:
            video_id = yt_match.group(1)
            media_embed = f'<iframe class="w-full aspect-video rounded-xl shadow-lg mb-4" src="https://www.youtube.com/embed/{video_id}" frameborder="0" allowfullscreen></iframe>'
            
        return imagen_url, media_embed
    except Exception as e:
        print(f"Error extrayendo HTML de {url}: {e}")
        return None, None

def extraer_imagen_rss(entry):
    if 'media_content' in entry and len(entry.media_content) > 0:
        return entry.media_content[0]['url']
    if 'enclosures' in entry:
        for enc in entry.enclosures:
            if 'image' in enc.get('type', ''):
                return enc.get('href')
    img_match = re.search(r'<img[^>]+src="([^">]+)"', entry.get('description', ''))
    if img_match:
        return img_match.group(1)
    return None

async def generar_audio_mp3(texto_audio, filename="temp_audio.mp3"):
    communicate = edge_tts.Communicate(texto_audio, VOZ_ARGENTINA)
    await communicate.save(filename)
    return filename

def subir_audio_a_supabase(file_path, destination_name):
    try:
        with open(file_path, 'rb') as f:
            supabase.storage.from_('audios').upload(
                path=destination_name,
                file=f,
                file_options={"content-type": "audio/mpeg", "x-upsert": "true"}
            )
        return supabase.storage.from_('audios').get_public_url(destination_name)
    except Exception as e:
        print(f"Error subiendo audio: {e}")
        return None

def procesar_noticia_con_gemini(titulo, descripcion):
    model = genai.GenerativeModel("gemini-3.8-flash")
    prompt = f"""
    Sos el editor principal de un portal de noticias automatizado.
    Procesá la noticia y devolvé ÚNICAMENTE un objeto JSON válido.
    
    Campos requeridos:
    - "titulo": Título claro y atractivo.
    - "categoria": Elegí STRICTAMENTE una: ["Deportes", "Policiales", "Cultura", "Política", "Sociedad", "Economía", "Nacionales", "Mundo", "Alertas"]. Si la noticia es sobre una catástrofe, muerte de un famoso o atentado, usa "Alertas".
    - "puntos_clave": Viñetas en Markdown. Usa solo asteriscos simples (*). No uses saltos de línea con '\\n'.
    - "texto_audio": Resumen ultra breve de máximo 2 oraciones para leer en 15 segundos.

    Título original: {titulo}
    Texto: {descripcion}
    """
    try:
        response = model.generate_content(prompt)
        texto_limpio = response.text.replace("```json", "").replace("```", "").strip()
        return json.loads(texto_limpio)
    except Exception as e:
        print(f"Error de IA: {e}")
        return None

async def ejecutar_bot():
    print("Iniciando escaneo masivo (Locales, Nacionales y Alertas)...")
    for feed_url in FEEDS:
        try:
            respuesta = requests.get(feed_url, headers=HEADERS, timeout=15)
            feed = feedparser.parse(respuesta.content)
            
            print(f"Escaneando feed: {feed_url} ({len(feed.entries)} noticias encontradas)")

            for entry in feed.entries[:3]: # Procesar las 3 más recientes por feed
                fuente_url = entry.link
                
                res = supabase.from_('noticias').select('id').eq('fuente_url', fuente_url).execute()
                if res.data and len(res.data) > 0:
                    continue

                titulo_orig = entry.title
                resumen_orig = entry.get('summary', entry.get('description', ''))
                
                data_ia = procesar_noticia_con_gemini(titulo_orig, resumen_orig)
                if not data_ia:
                    continue

                # Freno para no saturar la API gratuita de Gemini
                time.sleep(5)

                imagen_real, video_real = obtener_metadatos_html(fuente_url)
                if not imagen_real:
                    imagen_real = extraer_imagen_rss(entry)

                audio_url = None
                if data_ia.get("texto_audio"):
                    temp_file = f"audio_{abs(hash(fuente_url))}.mp3"
                    await generar_audio_mp3(data_ia["texto_audio"], temp_file)
                    audio_url = subir_audio_a_supabase(temp_file, f"noticia_{abs(hash(fuente_url))}.mp3")
                    if os.path.exists(temp_file): os.remove(temp_file)

                nueva_noticia = {
                    "titulo": data_ia["titulo"],
                    "puntos_clave": data_ia["puntos_clave"],
                    "categoria": data_ia.get("categoria", "Informativa"),
                    "fuente_url": fuente_url,
                    "audio_url": audio_url,
                    "media_embed": video_real,
                    "imagen_url": imagen_real
                }

                supabase.from_('noticias').insert(nueva_noticia).execute()
                print(f"✅ Publicada [{data_ia.get('categoria')}]: {data_ia['titulo']}")
        except Exception as e:
            print(f"Error en {feed_url}: {e}")

if __name__ == "__main__":
    asyncio.run(ejecutar_bot())
