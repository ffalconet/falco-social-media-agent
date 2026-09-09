# FALCO Social Media Agent

Backend de production média pour **FALCO — Fly on your own**.

Ce projet fournit les briques techniques utilisées par l’agent FALCO pour produire des contenus social media cohérents avec l’identité de marque : génération d’images, génération vidéo, suivi des opérations Veo, publication des clips, montage vertical, textes, transitions et end-card FALCO.

> FALCO ne vend pas la performance sportive.  
> FALCO vend la cohérence personnelle.  
> **Fly on your own.**

## Objectif

Transformer un concept validé en contenu social media exploitable avec le minimum d’intervention manuelle.

```text
Concept validé
→ Image maître
→ Keyframes cohérentes
→ Image-to-video
→ Publication des clips
→ Montage
→ Textes + transitions + end-card
→ Vidéo finale
→ Validation humaine
```

## Architecture

- **Backend** : Python, Flask, Gunicorn, Google Cloud Run
- **Image** : Gemini Image
- **Vidéo** : Veo 3.1 Lite
- **Stockage** : Google Cloud Storage
- **Montage** : FFmpeg
- **Format cible** : 1080×1920, 30 fps, H.264
- **Typographie** : Luciole
- **End-card** : `falco_premium_v1`

Bucket actuel :

```text
falco-video-output-549259282828
```

Organisation principale :

```text
falco/
├── images/
├── montages/
├── assets/
│   └── falco-logo.png
└── <video_id>.mp4
```

## Endpoints

### `GET /health`
Vérifie que le service est opérationnel.

### `POST /generate-image`
Génère une image FALCO. Sans `reference_image`, crée une image maître. Avec `reference_image`, crée une keyframe cohérente.

```json
{
  "prompt": "Create a cinematic FALCO keyframe...",
  "reference_image": "falco/images/master.jpg"
}
```

### `GET /image/{image_id}`
Retourne une image générée via le proxy Cloud Run.

### `POST /generate`
Lance une génération Veo en text-to-video ou image-to-video.

```json
{
  "prompt": "...",
  "aspect_ratio": "9:16",
  "image_object_name": "falco/images/keyframe.jpg"
}
```

### `GET /status`
Suit une opération Veo via `operation_name`.

### `POST /publish`
Télécharge une vidéo Veo terminée, la stocke dans GCS et retourne une URL stable via Cloud Run.

### `GET /video/{video_id}`
Retourne une vidéo publiée via le proxy Cloud Run.

### `POST /montage-simple`
Assemble les clips, ajoute textes, transitions et end-card. Supporte aussi un fallback image vers mini-plan vidéo FFmpeg.

Exemple vidéo :

```json
{
  "type": "video",
  "filename": "falco/clip.mp4",
  "start": 0,
  "duration": 3
}
```

Exemple fallback image :

```json
{
  "type": "image",
  "filename": "falco/images/keyframe.jpg",
  "start": 0,
  "duration": 3
}
```

Transition :

```json
{
  "type": "crossfade",
  "duration": 0.20
}
```

## Continuité FALCO

Pour un personnage récurrent :

```text
IMAGE MAÎTRE
├── KEYFRAME 01
├── KEYFRAME 02
└── KEYFRAME 03
```

Chaque keyframe doit dériver **directement du master**, jamais d’une keyframe précédente.

À préserver :
- identité et visage ;
- âge apparent ;
- cheveux / barbe ;
- morphologie ;
- tenue ;
- environnement ;
- lumière ;
- colorimétrie ;
- objets narratifs.

## Identité visuelle

```text
Blue       #4173ff
Cream      #f7efe4
Black      #363636
Light Grey #f9fafb
```

Police : **Luciole**

## Variables d’environnement

```text
GEMINI_API_KEY
FALCO_API_KEY
PORT
```

Ne jamais committer de secrets.

## Authentification

Les endpoints privés utilisent :

```http
Authorization: Bearer <FALCO_API_KEY>
```

## Développement local

```bash
python -m pip install -r requirements.txt
gcloud auth application-default login
python -m py_compile app.py
python app.py
```

Vérification FFmpeg :

```bash
ffmpeg -version
ffprobe -version
```

## Déploiement

Service Cloud Run :

```text
falco-video-api
```

Région :

```text
europe-west9
```

URL :

```text
https://falco-video-api-549259282828.europe-west9.run.app
```

```text
gcloud run deploy falco-video-api --source . --region europe-west9 --allow-unauthenticated --memory 2Gi --cpu 2  --concurrency 1 --timeout 900 --set-secrets="GEMINI_API_KEY=gemini-api-key:latest,FALCO_API_KEY=falco-api-key:latest"
```

## Gestion des erreurs

Règle fondamentale :

```text
ne jamais régénérer tout un post lorsqu’un seul plan échoue
```

Stratégie cible :
1. conserver master et keyframes ;
2. retry uniquement le plan concerné ;
3. ne pas dupliquer les générations ;
4. fallback image FFmpeg si Veo échoue de façon persistante ;
5. poursuivre avec les assets déjà réussis.

## Limite actuelle principale

Le pipeline média fonctionne, mais l’autonomie complète nécessite un **état persistant par production**.

Le GPT peut encore perdre entre deux tours :
- `operation_name` ;
- `object_name` ;
- `filename` ;
- statut d’un plan ;
- statut global.

Prochaine évolution :

```text
production_id
├── master
├── keyframes
├── veo_operations
├── published_clips
├── montage
└── status
```

## Roadmap

1. **Production state** : persistance via `production_id`
2. **Orchestrateur** : polling, retries, publish, fallback, montage
3. **Validation humaine** : 🟢 VALIDER / 🟠 MODIFIER / 🔴 REJETER
4. **Publication** : Instagram / TikTok
5. **Analytics** : vues, rétention, partages, sauvegardes, clics, ventes

## Sécurité

Ne jamais ajouter au dépôt :
- clés Gemini ;
- clés FALCO ;
- credentials GCP ;
- fichiers JSON de service account ;
- signed URLs ;
- secrets Shopify / Meta / TikTok.

## Marque

FALCO est une marque sport premium minimaliste construite autour de la discipline personnelle.

> Si tu as dit que tu le ferais, fais-le.

La marque ne célèbre pas la motivation.  
Elle célèbre la constance, le standard interne et les engagements tenus lorsque personne ne regarde.

**FALCO — Fly on your own.**