#!/usr/bin/env python3
"""Generate a realistic 300-bookmark Netscape HTML file for testing the organizer."""
import random
from datetime import datetime, timedelta

CATEGORIES = {
    "AI & Machine Learning": [
        ("Hugging Face", "https://huggingface.co/"),
        ("Ollama", "https://ollama.com/"),
        ("LM Studio", "https://lmstudio.ai/"),
        ("MLX Examples", "https://github.com/ml-explore/mlx-examples"),
        ("Anthropic Docs", "https://docs.anthropic.com/"),
        ("OpenAI Platform", "https://platform.openai.com/"),
        ("LiteLLM Docs", "https://docs.litellm.ai/"),
        ("vLLM Project", "https://github.com/vllm-project/vllm"),
        ("LocalAI", "https://localai.io/"),
        ("Stable Diffusion WebUI", "https://github.com/AUTOMATIC1111/stable-diffusion-webui"),
        ("ComfyUI", "https://github.com/comfyanonymous/ComfyUI"),
        ("LangChain Docs", "https://python.langchain.com/docs/"),
        ("Papers With Code", "https://paperswithcode.com/"),
        ("Weights & Biases", "https://wandb.ai/"),
        ("GGUF Models", "https://huggingface.co/TheBloke"),
        ("ExLlamaV2", "https://github.com/turboderp/exllamav2"),
        ("Transformers Docs", "https://huggingface.co/docs/transformers/"),
        ("PyTorch Docs", "https://pytorch.org/docs/stable/"),
        ("MLX Framework", "https://ml-explore.github.io/mlx/"),
        ("Qwen Models", "https://qwen.ai/"),
        ("Claude API Reference", "https://docs.anthropic.com/en/api/"),
        ("Mistral AI", "https://mistral.ai/"),
        ("Together AI", "https://www.together.ai/"),
        ("AI News Daily", "https://www.aidaily.co.uk/"),
        ("r/LocalLLaMA", "https://www.reddit.com/r/LocalLLaMA/"),
    ],
    "Linux & Fedora": [
        ("Fedora Magazine", "https://fedoramagazine.org/"),
        ("Fedora Discussion", "https://discussion.fedoraproject.org/"),
        ("Fedora Packages", "https://packages.fedoraproject.org/"),
        ("RPM Fusion", "https://rpmfusion.org/"),
        ("COPR Repos", "https://copr.fedorainfracloud.org/"),
        ("Fedora Alt Downloads", "https://alt.fedoraproject.org/"),
        ("Ask Fedora", "https://ask.fedoraproject.org/"),
        ("Fedora Labs", "https://labs.fedoraproject.org/"),
        ("Fedora Developer Portal", "https://developer.fedoraproject.org/"),
        ("Planet Fedora", "https://planet.fedoraproject.org/"),
        ("Arch Wiki", "https://wiki.archlinux.org/"),
        ("KDE Neon", "https://neon.kde.org/"),
        ("KDE Store", "https://store.kde.org/"),
        ("GNOME Extensions", "https://extensions.gnome.org/"),
        ("Flathub", "https://flathub.org/"),
        ("Podman Docs", "https://docs.podman.io/"),
        ("Cockpit Project", "https://cockpit-project.org/"),
        ("Tailscale Docs", "https://tailscale.com/kb/"),
        ("WireGuard", "https://www.wireguard.com/"),
        ("Phoronix", "https://www.phoronix.com/"),
        ("DistroWatch", "https://distrowatch.com/"),
        ("Linux Kernel Newbies", "https://kernelnewbies.org/"),
        ("ProtonDB", "https://www.protondb.com/"),
        ("Lutris", "https://lutris.net/"),
        ("GamingOnLinux", "https://www.gamingonlinux.com/"),
    ],
    "Development": [
        ("GitHub", "https://github.com/"),
        ("Stack Overflow", "https://stackoverflow.com/"),
        ("Swift Package Index", "https://swiftpackageindex.com/"),
        ("Apple Developer Docs", "https://developer.apple.com/documentation/"),
        ("SwiftUI Tutorials", "https://developer.apple.com/tutorials/swiftui"),
        ("Hacking with Swift", "https://www.hackingwithswift.com/"),
        ("Ray Wenderlich", "https://www.kodeco.com/"),
        ("Python Docs", "https://docs.python.org/3/"),
        ("FastAPI Docs", "https://fastapi.tiangolo.com/"),
        ("TypeScript Handbook", "https://www.typescriptlang.org/docs/handbook/"),
        ("MDN Web Docs", "https://developer.mozilla.org/"),
        ("Can I Use", "https://caniuse.com/"),
        ("Tailwind CSS", "https://tailwindcss.com/docs"),
        ("Next.js Docs", "https://nextjs.org/docs"),
        ("Vercel", "https://vercel.com/"),
        ("Netlify", "https://www.netlify.com/"),
        ("Docker Hub", "https://hub.docker.com/"),
        ("Kubernetes Docs", "https://kubernetes.io/docs/"),
        ("Terraform Registry", "https://registry.terraform.io/"),
        ("Regex101", "https://regex101.com/"),
        ("JSON Formatter", "https://jsonformatter.org/"),
        ("Postman", "https://www.postman.com/"),
        ("npm Registry", "https://www.npmjs.com/"),
        ("PyPI", "https://pypi.org/"),
        ("Homebrew Formulae", "https://formulae.brew.sh/"),
        ("GitLab", "https://gitlab.com/"),
        ("Bitbucket", "https://bitbucket.org/"),
        ("Cloudflare Docs", "https://developers.cloudflare.com/"),
        ("DigitalOcean Tutorials", "https://www.digitalocean.com/community/tutorials"),
        ("FreeCodeCamp", "https://www.freecodecamp.org/"),
    ],
    "Self-Hosting & NAS": [
        ("UGREEN NAS Community", "https://community.ugnas.com/"),
        ("Portainer", "https://www.portainer.io/"),
        ("Nextcloud", "https://nextcloud.com/"),
        ("Jellyfin", "https://jellyfin.org/"),
        ("Plex", "https://www.plex.tv/"),
        ("Home Assistant", "https://www.home-assistant.io/"),
        ("Traefik Docs", "https://doc.traefik.io/traefik/"),
        ("Nginx Proxy Manager", "https://nginxproxymanager.com/"),
        ("Pi-hole", "https://pi-hole.net/"),
        ("AdGuard Home", "https://adguard.com/en/adguard-home/overview.html"),
        ("Grafana", "https://grafana.com/"),
        ("Prometheus", "https://prometheus.io/"),
        ("Uptime Kuma", "https://github.com/louislam/uptime-kuma"),
        ("Watchtower", "https://containrrr.dev/watchtower/"),
        ("Immich Photos", "https://immich.app/"),
        ("Paperless-ngx", "https://docs.paperless-ngx.com/"),
        ("Vaultwarden", "https://github.com/dani-garcia/vaultwarden"),
        ("Authentik", "https://goauthentik.io/"),
        ("Syncthing", "https://syncthing.net/"),
        ("Duplicati", "https://www.duplicati.com/"),
        ("r/selfhosted", "https://www.reddit.com/r/selfhosted/"),
        ("Awesome Self-Hosted", "https://awesome-selfhosted.net/"),
        ("LinuxServer.io", "https://www.linuxserver.io/"),
        ("TrueNAS Docs", "https://www.truenas.com/docs/"),
        ("Unraid Forums", "https://forums.unraid.net/"),
    ],
    "Anime & Manga": [
        ("MyAnimeList", "https://myanimelist.net/"),
        ("AniList", "https://anilist.co/"),
        ("Crunchyroll", "https://www.crunchyroll.com/"),
        ("Funimation", "https://www.funimation.com/"),
        ("MangaDex", "https://mangadex.org/"),
        ("Anime News Network", "https://www.animenewsnetwork.com/"),
        ("r/anime", "https://www.reddit.com/r/anime/"),
        ("r/manga", "https://www.reddit.com/r/manga/"),
        ("Kitsu", "https://kitsu.app/"),
        ("Anime Planet", "https://www.anime-planet.com/"),
        ("Aniwave", "https://aniwave.to/"),
        ("Nyaa.si", "https://nyaa.si/"),
        ("Anime Corner", "https://animecorner.me/"),
        ("MAL Reviews", "https://myanimelist.net/reviews.php"),
        ("Anime Trending", "https://anitrendz.com/"),
        ("Jujutsu Kaisen Wiki", "https://jujutsu-kaisen.fandom.com/"),
        ("One Piece Wiki", "https://onepiece.fandom.com/"),
        ("Chainsaw Man Wiki", "https://chainsaw-man.fandom.com/"),
        ("Solo Leveling", "https://sololeveling.fandom.com/"),
        ("Demon Slayer Wiki", "https://kimetsu-no-yaiba.fandom.com/"),
    ],
    "Gaming": [
        ("Steam", "https://store.steampowered.com/"),
        ("SteamDB", "https://steamdb.info/"),
        ("PC Gaming Wiki", "https://www.pcgamingwiki.com/"),
        ("IGN", "https://www.ign.com/"),
        ("Kotaku", "https://kotaku.com/"),
        ("r/pcgaming", "https://www.reddit.com/r/pcgaming/"),
        ("IsThereAnyDeal", "https://isthereanydeal.com/"),
        ("HowLongToBeat", "https://howlongtobeat.com/"),
        ("GG.deals", "https://gg.deals/"),
        ("Nexus Mods", "https://www.nexusmods.com/"),
        ("GeForce NOW", "https://www.nvidia.com/en-us/geforce-now/"),
        ("AMD Adrenalin", "https://www.amd.com/en/products/software/adrenalin.html"),
        ("Crimson Desert", "https://www.crimsondesert.com/"),
        ("Elden Ring Wiki", "https://eldenring.wiki.fextralife.com/"),
        ("Baldur's Gate 3 Wiki", "https://bg3.wiki/"),
        ("r/linux_gaming", "https://www.reddit.com/r/linux_gaming/"),
        ("Moonlight Streaming", "https://moonlight-stream.org/"),
        ("RetroArch", "https://www.retroarch.com/"),
        ("RPCS3", "https://rpcs3.net/"),
        ("Ryujinx", "https://ryujinx.org/"),
    ],
    "News & Tech": [
        ("Hacker News", "https://news.ycombinator.com/"),
        ("Ars Technica", "https://arstechnica.com/"),
        ("The Verge", "https://www.theverge.com/"),
        ("TechCrunch", "https://techcrunch.com/"),
        ("Lobsters", "https://lobste.rs/"),
        ("Slashdot", "https://slashdot.org/"),
        ("AnandTech", "https://www.anandtech.com/"),
        ("Tom's Hardware", "https://www.tomshardware.com/"),
        ("Wired", "https://www.wired.com/"),
        ("9to5Mac", "https://9to5mac.com/"),
        ("MacRumors", "https://www.macrumors.com/"),
        ("AppleInsider", "https://appleinsider.com/"),
        ("Daring Fireball", "https://daringfireball.net/"),
        ("r/technology", "https://www.reddit.com/r/technology/"),
        ("Product Hunt", "https://www.producthunt.com/"),
        ("TorrentFreak", "https://torrentfreak.com/"),
        ("Bleeping Computer", "https://www.bleepingcomputer.com/"),
        ("Krebs on Security", "https://krebsonsecurity.com/"),
        ("EFF", "https://www.eff.org/"),
        ("arstechnica/gadgets", "https://arstechnica.com/gadgets/"),
    ],
    "Shopping & Deals": [
        ("Amazon", "https://www.amazon.com/"),
        ("eBay", "https://www.ebay.com/"),
        ("Newegg", "https://www.newegg.com/"),
        ("B&H Photo", "https://www.bhphotovideo.com/"),
        ("AliExpress", "https://www.aliexpress.com/"),
        ("Slickdeals", "https://slickdeals.net/"),
        ("r/buildapcsales", "https://www.reddit.com/r/buildapcsales/"),
        ("PCPartPicker", "https://pcpartpicker.com/"),
        ("Camel Camel Camel", "https://camelcamelcamel.com/"),
        ("Monoprice", "https://www.monoprice.com/"),
        ("iFixit", "https://www.ifixit.com/"),
        ("Framework Laptop", "https://frame.work/"),
        ("System76", "https://system76.com/"),
        ("MicroCenter", "https://www.microcenter.com/"),
        ("Crucial Memory", "https://www.crucial.com/"),
    ],
    "Books & Learning": [
        ("Kindle Cloud Reader", "https://read.amazon.com/"),
        ("Libgen", "https://libgen.is/"),
        ("Z-Library", "https://z-lib.io/"),
        ("Anna's Archive", "https://annas-archive.org/"),
        ("Goodreads", "https://www.goodreads.com/"),
        ("O'Reilly Learning", "https://www.oreilly.com/"),
        ("MIT OpenCourseWare", "https://ocw.mit.edu/"),
        ("Khan Academy", "https://www.khanacademy.org/"),
        ("Coursera", "https://www.coursera.org/"),
        ("Udemy", "https://www.udemy.com/"),
        ("Project Gutenberg", "https://www.gutenberg.org/"),
        ("Standard Ebooks", "https://standardebooks.org/"),
        ("Calibre Web", "https://github.com/janeczku/calibre-web"),
        ("Audiobookshelf", "https://www.audiobookshelf.org/"),
        ("OpenLibrary", "https://openlibrary.org/"),
    ],
    "Productivity & Tools": [
        ("Obsidian", "https://obsidian.md/"),
        ("Notion", "https://www.notion.so/"),
        ("Raindrop.io", "https://raindrop.io/"),
        ("Bitwarden", "https://bitwarden.com/"),
        ("Proton Mail", "https://proton.me/mail"),
        ("Todoist", "https://todoist.com/"),
        ("Excalidraw", "https://excalidraw.com/"),
        ("draw.io", "https://app.diagrams.net/"),
        ("Figma", "https://www.figma.com/"),
        ("Canva", "https://www.canva.com/"),
        ("Photopea", "https://www.photopea.com/"),
        ("TinyPNG", "https://tinypng.com/"),
        ("remove.bg", "https://www.remove.bg/"),
        ("DeepL Translate", "https://www.deepl.com/"),
        ("Speedtest", "https://www.speedtest.net/"),
    ],
    "Social & Forums": [
        ("Reddit", "https://www.reddit.com/"),
        ("Twitter/X", "https://x.com/"),
        ("Mastodon", "https://mastodon.social/"),
        ("Discord", "https://discord.com/"),
        ("Lemmy", "https://lemmy.world/"),
        ("Hacker News", "https://news.ycombinator.com/"),
        ("r/homelab", "https://www.reddit.com/r/homelab/"),
        ("r/datahoarder", "https://www.reddit.com/r/DataHoarder/"),
        ("r/unraid", "https://www.reddit.com/r/unRAID/"),
        ("r/Fedora", "https://www.reddit.com/r/Fedora/"),
        ("r/iOSProgramming", "https://www.reddit.com/r/iOSProgramming/"),
        ("r/SwiftUI", "https://www.reddit.com/r/SwiftUI/"),
        ("r/MachineLearning", "https://www.reddit.com/r/MachineLearning/"),
        ("dev.to", "https://dev.to/"),
        ("Medium", "https://medium.com/"),
    ],
    "Spiritual & Wellness": [
        ("Insight Timer", "https://insighttimer.com/"),
        ("Headspace", "https://www.headspace.com/"),
        ("Yoga Journal", "https://www.yogajournal.com/"),
        ("Spirit Science", "https://thespiritscience.net/"),
        ("Gaia", "https://www.gaia.com/"),
        ("Daily Om", "https://www.dailyom.com/"),
        ("Mindful", "https://www.mindful.org/"),
        ("Tricycle", "https://tricycle.org/"),
        ("Access to Insight", "https://www.accesstoinsight.org/"),
        ("Dharma Seed", "https://dharmaseed.org/"),
    ],
}

def generate_html(target_count=300):
    # Flatten all bookmarks
    all_bm = []
    for cat, items in CATEGORIES.items():
        for title, url in items:
            all_bm.append((title, url, cat))
    
    # We have ~300 total, pad with variations if needed
    while len(all_bm) < target_count:
        cat = random.choice(list(CATEGORIES.keys()))
        base_title, base_url = random.choice(CATEGORIES[cat])
        suffix = random.randint(100, 999)
        all_bm.append((f"{base_title} - Page {suffix}", f"{base_url}page/{suffix}", cat))
    
    all_bm = all_bm[:target_count]
    random.shuffle(all_bm)
    
    # Build Netscape HTML
    base_ts = int(datetime(2024, 1, 1).timestamp())
    lines = [
        '<!DOCTYPE NETSCAPE-Bookmark-file-1>',
        '<!-- This is an automatically generated file.',
        '     It will be read and overwritten.',
        '     DO NOT EDIT! -->',
        '<META HTTP-EQUIV="Content-Type" CONTENT="text/html; charset=UTF-8">',
        '<TITLE>Bookmarks</TITLE>',
        '<H1>Bookmarks</H1>',
        '<DL><p>',
    ]
    
    # Group by category for folder structure
    by_cat = {}
    for title, url, cat in all_bm:
        by_cat.setdefault(cat, []).append((title, url))
    
    # Some go in folders, some are unfiled (realistic)
    unfiled = []
    for cat, items in by_cat.items():
        # Put 80% in folders, 20% unfiled
        split = int(len(items) * 0.8)
        folder_items = items[:split]
        unfiled.extend([(t, u, cat) for t, u in items[split:]])
        
        if folder_items:
            ts = base_ts + random.randint(0, 30000000)
            lines.append(f'    <DT><H3 ADD_DATE="{ts}" LAST_MODIFIED="{ts + 1000}">{cat}</H3>')
            lines.append('    <DL><p>')
            for title, url in folder_items:
                ts2 = base_ts + random.randint(0, 50000000)
                lines.append(f'        <DT><A HREF="{url}" ADD_DATE="{ts2}">{title}</A>')
            lines.append('    </DL><p>')
    
    # Add unfiled bookmarks at root level
    if unfiled:
        lines.append('    <DT><H3>Unfiled</H3>')
        lines.append('    <DL><p>')
        for title, url, _ in unfiled:
            ts3 = base_ts + random.randint(0, 50000000)
            lines.append(f'        <DT><A HREF="{url}" ADD_DATE="{ts3}">{title}</A>')
        lines.append('    </DL><p>')
    
    lines.append('</DL><p>')
    return '\n'.join(lines), len(all_bm)

html, count = generate_html(300)
outpath = "/sessions/sleepy-gifted-wozniak/mnt/agent/safari_bookmarks_300.html"
with open(outpath, "w") as f:
    f.write(html)
print(f"Generated {count} bookmarks → {outpath}")
print(f"File size: {len(html)} bytes")
print(f"Categories: {len(CATEGORIES)}")
for cat, items in CATEGORIES.items():
    print(f"  {cat}: {len(items)} bookmarks")
