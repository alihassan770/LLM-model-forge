# Model Forge: the application and the engine that builds its models, in one
# image.
#
# ONE image on purpose. A build currently runs inside the web request, so the
# process serving the page is the process that runs Blender - which means the
# web service needs Blender, and Nixpacks cannot install it. Splitting the
# build onto a queue and a second service is the right end state and is written
# up in DEPLOY.md, but it is a code change, not a deployment one, and this gets
# the thing running first.
#
# The same image serves both roles when that split happens: the start command
# decides whether it is `serve` or `worker`.

FROM python:3.12-slim-bookworm

# Pinned exactly. A Blender upgrade can change how an exporter names objects,
# and object names are the product here - every part has to stay separately
# selectable with the name the specification gave it. That is a decision to
# take deliberately, with a rebuild to look at, not one to inherit from
# whatever is current on the day the image is built.
ARG BLENDER_VERSION=4.2.5
ARG BLENDER_SERIES=4.2

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    FORGE_BLENDER=/opt/blender/blender

# ---------------------------------------------------------------------------
# System packages
# ---------------------------------------------------------------------------
#
# The X11 and OpenGL libraries are needed EVEN THOUGH no window is ever opened.
# Blender links against them at start-up whatever the mode, and without them
# `blender --background` dies with a loader error that reads like a corrupt
# download rather than a missing package. That cost an afternoon once.
#
# poppler-utils is the PDF reader: pdfinfo, pdftotext and pdftoppm, called over
# stdin and stdout by forge/ingest/pdf.py.
#
# libjpeg and libwebp back Pillow, which crops question regions and downscales
# rendered PDF pages.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl xz-utils ca-certificates \
        libx11-6 libxi6 libxxf86vm1 libxfixes3 libxrender1 libxkbcommon0 \
        libgl1 libglu1-mesa libegl1 libgomp1 libsm6 \
        libfreetype6 libpng16-16 libtiff6 libopenjp2-7 \
        poppler-utils libjpeg62-turbo libwebp7 zlib1g \
    && rm -rf /var/lib/apt/lists/*

# ---------------------------------------------------------------------------
# Blender
# ---------------------------------------------------------------------------
#
# Downloaded and unpacked rather than installed from apt: Debian's Blender is
# older and not pinnable, and this is one file from blender.org.
#
# The locale directory is 51 MB of interface translations for a program that
# never shows an interface here, so it goes. Nothing else is trimmed:
# `scripts/addons` holds the FBX, OBJ, glTF and STL exporters, and
# `python/lib` holds the numpy the glTF exporter imports, so both are load
# bearing despite looking like fat.
RUN curl -fsSL \
      "https://download.blender.org/release/Blender${BLENDER_SERIES}/blender-${BLENDER_VERSION}-linux-x64.tar.xz" \
      | tar -xJ -C /opt \
    && mv "/opt/blender-${BLENDER_VERSION}-linux-x64" /opt/blender \
    && rm -rf "/opt/blender/${BLENDER_SERIES}/datafiles/locale" \
    && ln -s /opt/blender/blender /usr/local/bin/blender \
    && apt-get purge -y curl xz-utils \
    && rm -rf /var/lib/apt/lists/*

RUN blender --version && pdftoppm -v && pdftotext -v

# Two messages in the build log are expected and harmless, noted so nobody
# spends time on them:
#
#   "BLT_lang_init: 'locale' data path for translations not found"
#       the translations deleted above. Blender carries on in English.
#   "EGL Error (0x3009): EGL_BAD_MATCH"
#       no GPU in the container. Workbench falls back to software rendering
#       and the preview PNGs are still written - the smoke test below proves
#       it by rendering one and checking the file.
#   "could not get a list of mounted file-systems"
#       Blender probing for drives it does not need.

# ---------------------------------------------------------------------------
# The application
# ---------------------------------------------------------------------------
WORKDIR /app

# `forge` is copied before the install because setuptools discovers packages
# matching "forge*" from pyproject.toml, so installing without it present
# either fails or produces an empty wheel. The cost is that editing a template
# reinstalls the dependencies; the Blender layer above is the slow one and it
# stays cached.
COPY pyproject.toml README.md ./
COPY forge ./forge
COPY run.py ./
COPY scripts/blender_smoke.py scripts/start.sh ./scripts/
RUN pip install --no-cache-dir .

# Prove Blender can do the job, at build time. This exports a cube to all five
# formats and renders a frame: if a shared library is missing or the exporters
# were stripped, the build fails here instead of the first user's model failing
# three days later.
#
# It has to be a real export. `hasattr(bpy.ops.export_scene, "fbx")` passes for
# operators that do not exist, because bpy.ops fabricates attributes on demand,
# and a check that cannot fail is worse than no check.
# mkdir here as well as in the script. The script creates it too, but a build
# step that depends on a Python file having been updated is a build step that
# fails confusingly when a layer is cached or an older copy is checked out.
RUN mkdir -p /tmp/forge-smoke \
    && blender --background --factory-startup \
        --python scripts/blender_smoke.py -- /tmp/forge-smoke \
    && rm -rf /tmp/forge-smoke

# Not root. Nothing here needs it, and a build runs a subprocess over
# user-supplied input, which is exactly where it matters.
#
# uid 1000 is stated rather than left to useradd, because a volume mounted by
# the platform is owned by a uid, and "chown 1000" is the instruction you will
# need if the volume turns out to be root-owned.
RUN useradd --create-home --uid 1000 --shell /bin/bash forge \
    && mkdir -p /app/.storage \
    && chown -R forge:forge /app
USER forge

# Railway sets PORT. The default is for `docker run -p 8000:8000` locally.
ENV PORT=8000
EXPOSE 8000

# Railway's healthcheckPath hits /healthz, which reports the database honestly
# rather than answering 200 while it is unreachable.
CMD ["sh", "scripts/start.sh"]
