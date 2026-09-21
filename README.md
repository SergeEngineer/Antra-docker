<p align="center">
  <img src="assets/antra-header.svg" width="100%" alt="Antra"/>
</p>

<p align="center">
  <sub>🌐 Running live at <a href="https://antra.hoshi.cfd/">antra.hoshi.cfd</a></sub>
</p>

<p align="center">
  <strong>A docker version of music library builder that turns Spotify, YouTube Music, Apple Music, Amazon Music, Tidal, Qobuz, and Deezer links into a fully tagged local library in FLAC, ALAC, AAC, or MP3.</strong>
</p>

---

## What it is

Antra is a docker version of music library manager. It helps you bring tracks into a tidy local library — automatically tagged with full metadata (title, artist, album, artwork, genre, and lyrics) and filed into a clean `Artist / Album` folder structure that works out of the box with media servers like Navidrome, Jellyfin, and Plex.

```
Formats:  FLAC · ALAC · AAC · MP3
Output:   Auto-tagged · artwork + lyrics · media-server ready
```

→ **[Feature guide](FEATURES.md)**

---

## Install

Clone the repository on your Unraid machine or your docker server
```
git clone https://github.com/SergeEngineer/Antra-docker
cd Antra-docker
```

Build the image from the root `Antra-docker` folder and specifying docker file as `-f docker/antra-web/Dockerfile `
```
docker build -f docker/antra-web/Dockerfile -t antra-web:latest .

docker build --no-cache --progress=plain -f docker/antra-web/Dockerfile -t antra-web:latest .
```

``` 
# docker app structure
/app/
├── antra/
│   ├── __init__.py
│   ├── json_cli.py
│   └── ...
├── antra_shared/
├── antra-wails/
├── requirements-runtime.txt
└── web/
    └── main.py
```

Check if your image is there
```
docker images antra
```
Run it
```
docker run --rm -p 7337:7337 -v /mnt/user/appdata/antra:/config -v /mnt/user/media/music:/music antra:latest
```

---

## Quick start

1. Launch Antra and pick your music library folder on first run
2. Choose your preferred output format
3. Add a link
4. Press **Add to Library**

Everything is fetched, tagged, and filed into the right folder automatically.

---

## Disclaimer

This repository and its contents are provided strictly for educational and research purposes. The software is provided "as-is" without warranty of any kind, express or implied, as stated in the LICENSE file.

- No copyrighted content is hosted, stored, mirrored, or distributed by this repository.
- Users are solely responsible for ensuring that their use of this software is properly authorised and complies with all applicable laws, regulations, and third-party terms of service.
- This software is provided free of charge by the maintainer. If you paid a third party for access to this software in its original form from this repository, you may have been misled. Any redistribution or commercial use by third parties must comply with the terms of the repository license. No affiliation, endorsement, or support by the maintainer is implied unless explicitly stated in writing.
- Antra is an independent project. It is not affiliated with, endorsed by, or connected to any other project or version on other platforms that may share a similar name. The maintainer has no control over or responsibility for third-party projects.
- The author(s) disclaim all liability for any direct, indirect, incidental, or consequential damages arising from the use or misuse of this software. Users assume all risk associated with its use.
- If you are a copyright holder or authorised representative and believe this repository infringes upon your rights, please contact the maintainer with sufficient detail (including relevant URLs and proof of ownership). The matter will be promptly investigated and appropriate action taken, which may include removal of the referenced material.

---

## Shoutout to the Day Ones

Huge thanks to these folks for bringing new ideas, feedback, and testing the beta builds before everyone else:

<p align="center">
  <a href="https://github.com/rafaelmolivebh">@rafaelmolivebh</a> &nbsp;·&nbsp;
  <a href="https://github.com/robarnoldio">@robarnoldio</a> &nbsp;·&nbsp;
  <a href="https://github.com/mishrabiswajit">@mishrabiswajit</a>
</p>

---

<p align="center">
  <sub>Built with ❤️ by <a href="https://github.com/anandprtp">Hoshiyaar Singh</a> · <a href="https://github.com/anandprtp/Antra/issues">Report an Issue</a> · <a href="https://t.me/antraaverse">Telegram</a> · <a href="https://discord.com/invite/UcY5cqMuE">Discord</a></sub>
</p>
