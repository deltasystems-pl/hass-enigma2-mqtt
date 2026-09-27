"""A plugin release the signed index lists, fetched and verified before anything uses it.

The bundle is the one package this integration ships; everything else the update card, the
options flow's downgrade and the relay for a receiver without internet install is a release the
signed index lists (ADR-0008). This module turns one version of that index into bytes that may go
to a receiver, and refuses anything else - **before** a receiver is connected to, so a refusal
here leaves the receiver exactly as it was.

**What is fetched.** Only a version the last verified index lists, and only while the one rule
allows it: the same contract major, at or above the floor (the higher of this integration's and
the index's), this integration at or above the release's `min_integration`, not withdrawn. The
downgrade asks for an older version than the one running; the rule is the same, because the floor
and a withdrawal exist to keep people off a bad version in either direction.

**How it is fetched.** From the index's own origin, which serves the packages beside the index,
with the same verified TLS, no redirect, the same timeout, and a ceiling of the signed size - one
byte past it is read so that a longer file is refused as longer rather than judged cut short. The
bytes are believed only when their size and sha256 are the signed entry's and the control file
inside names this package and this version. When the bundle is those very bytes - the release
this integration shipped - they are taken from the bundle and nothing is downloaded.

**The second opinion** (defence in depth, OD 6 of the design): GitHub's API reports a `digest`
for every release asset, and it has to agree with the signed sha256. A disagreement refuses; an
API that cannot be reached, is rate-limited or has no digest to give is noted and does not refuse,
because the signature is the authority and the API only a cross-check. Its verdict is kept a day
per version, whoever asked: the relay can be asked for a package by anybody who can publish on the
broker, and that must not be a way to spend the 60 requests an hour GitHub allows an address.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import gzip
import hashlib
import io
import json
import logging
from pathlib import Path
import tarfile
from typing import Any

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import homeassistant.util.dt as dt_util

from . import bundle as bundle_module, release_index
from .buildid import BuildInfoError, _ar_members
from .bundle import BundledPlugin, BundleError
from .const import (
    DOMAIN,
    PLUGIN_CONTRACT,
    PLUGIN_DIGEST_CACHE,
    PLUGIN_INDEX_ORIGIN,
    PLUGIN_MIN_VERSION,
    PLUGIN_RELEASE_API,
    PLUGIN_RELEASE_API_LIMIT,
)
from .release_store import _async_get, _Fetched, async_release_index_cache

_LOGGER = logging.getLogger(__name__)

PACKAGE = release_index.PACKAGE

# Why a version cannot be had. Each is a sentence of its own in every place that says it.
NO_INDEX = "no_index"
UNKNOWN_VERSION = "unknown_version"
WITHDRAWN = "withdrawn"
BELOW_FLOOR = "below_floor"
INCOMPATIBLE = "incompatible"
# Anything that goes wrong between asking the origin and holding the signed bytes: unreachable,
# a redirect, an HTTP error, a file of the wrong size or checksum, or one that is not the package
# it says it is. One sentence covers them, the one the design gives the household (section
# ae.10); the log says which.
DOWNLOAD = "download"
# GitHub's own digest of the release asset disagrees with the signed index.
DIGEST = "digest"

# The digest cross-check's verdicts.
DIGEST_MATCH = "match"
DIGEST_UNAVAILABLE = "unavailable"
DIGEST_MISMATCH = "mismatch"

# Where a package's bytes came from.
ORIGIN_BUNDLE = "bundle"
ORIGIN_DOWNLOAD = "download"

_DIGEST_KEY = f"{DOMAIN}_release_digests"
_CONTROL_LIMIT = 64 * 1024


class PackageError(Exception):
    """A version that cannot be had, with the code that says why and what the sentence needs."""

    def __init__(self, code: str, detail: str, placeholders: dict[str, str]) -> None:
        """Keep the code for the sentence and the detail for the log."""
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.placeholders = placeholders


@dataclass(frozen=True, slots=True)
class PackageSource:
    """Verified package bytes, and what the signed entry says about them.

    What the SSH installer uploads and the relay serves. `commit` is the signed entry's commit,
    which the restart proof holds the receiver's `info.build` to; `depends` the packages the
    release needs, which the installer asks the receiver about before it changes anything.
    """

    version: str
    data: bytes = field(repr=False)
    sha256: str
    commit: str | None
    depends: tuple[str, ...]
    origin: str
    self_update: bool = False


def eligible_release(
    index: dict[str, Any] | None, version: str, *, integration_version: str | None
) -> dict[str, Any]:
    """The index's entry for `version`, when the rule lets this integration install it.

    Raises PackageError otherwise, naming the rule that refused.
    """
    placeholders = {"version": version}
    if index is None:
        raise PackageError(NO_INDEX, "no verified release index is held", placeholders)
    entry = release_index.release_of(index, version)
    if entry is None:
        raise PackageError(
            UNKNOWN_VERSION, f"the signed release index does not list {version}", placeholders
        )
    floor = release_index.effective_floor(index, PLUGIN_MIN_VERSION)
    reason = release_index.incompatibility(
        entry,
        contract=PLUGIN_CONTRACT,
        floor=floor,
        integration_version=integration_version,
    )
    if reason == release_index.REASON_WITHDRAWN:
        raise PackageError(
            WITHDRAWN,
            f"{version} is withdrawn: {entry['withdrawn']}",
            {**placeholders, "reason": str(entry["withdrawn"])},
        )
    if reason == release_index.REASON_BELOW_FLOOR:
        raise PackageError(
            BELOW_FLOOR, f"{version} is below the floor {floor}", {**placeholders, "floor": floor}
        )
    if reason is not None:
        raise PackageError(
            INCOMPATIBLE, f"{version} is not compatible with this integration ({reason})",
            placeholders,
        )
    return entry


def _control(data: bytes) -> dict[str, str]:
    """The fields of an IPK's control file, read and never run."""
    try:
        member = _ar_members(data).get("control.tar.gz")
    except BuildInfoError as error:
        raise ValueError(str(error)) from error
    if member is None:
        raise ValueError("the package has no control.tar.gz")
    try:
        with (
            gzip.GzipFile(fileobj=io.BytesIO(member)) as unzipped,
            tarfile.open(fileobj=unzipped, mode="r|") as archive,
        ):
            for item in archive:
                if item.name.removeprefix("./") != "control":
                    continue
                if not item.isfile() or item.size > _CONTROL_LIMIT:
                    raise ValueError("the control file is not a small regular file")
                handle = archive.extractfile(item)
                if handle is None:
                    raise ValueError("the control file cannot be read")
                text = handle.read().decode("utf-8")
                break
            else:
                raise ValueError("control.tar.gz has no control file")
    except (OSError, EOFError, tarfile.TarError, UnicodeDecodeError) as error:
        raise ValueError(f"control.tar.gz cannot be read: {error}") from error
    fields: dict[str, str] = {}
    for line in text.splitlines():
        name, separator, value = line.partition(":")
        if separator and name and not name[0].isspace():
            fields.setdefault(name.strip(), value.strip())
    return fields


def verify_package(data: bytes, entry: dict[str, Any]) -> None:
    """Refuse bytes that are not the signed entry's package: size, sha256, then control file.

    The checksum is the proof; the control file is read after it only so that a package whose
    signed entry was written wrong - its own name or version not what the index says - is
    caught here rather than by opkg on the receiver.
    """
    version = entry["version"]
    placeholders = {"version": version}
    if len(data) != entry["size"]:
        raise PackageError(
            DOWNLOAD,
            f"the package for {version} is {len(data)} bytes, the signed index says "
            f"{entry['size']}",
            placeholders,
        )
    if hashlib.sha256(data).hexdigest() != entry["sha256"]:
        raise PackageError(
            DOWNLOAD,
            f"the package for {version} does not have the sha256 the signed index names",
            placeholders,
        )
    try:
        control = _control(data)
    except ValueError as error:
        raise PackageError(
            DOWNLOAD, f"the package for {version} cannot be read: {error}", placeholders
        ) from error
    if control.get("Package") != PACKAGE or control.get("Version") != version:
        raise PackageError(
            DOWNLOAD,
            f"the package for {version} names itself {control.get('Package')!r} "
            f"{control.get('Version')!r}",
            placeholders,
        )


def _digests(hass: HomeAssistant) -> dict[tuple[str, str], tuple[str, datetime]]:
    return hass.data.setdefault(_DIGEST_KEY, {})


def _asset_digest(body: bytes, filename: str) -> str | None:
    """The `digest` GitHub reports for one asset of a release, or None when it gives none."""
    try:
        release = json.loads(body)
    except ValueError:
        return None
    assets = release.get("assets") if isinstance(release, dict) else None
    if not isinstance(assets, list):
        return None
    for asset in assets:
        if isinstance(asset, dict) and asset.get("name") == filename:
            digest = asset.get("digest")
            return digest if isinstance(digest, str) else None
    return None


async def async_digest_check(hass: HomeAssistant, entry: dict[str, Any]) -> str:
    """Hold the signed sha256 against the digest GitHub reports for the release asset.

    Returns DIGEST_MATCH or DIGEST_UNAVAILABLE, and raises PackageError(DIGEST) on a
    disagreement. The verdict is kept for a day per version and checksum, and a request that
    failed has spent its turn as well - retrying on every relay request is what a rate limit
    punishes.
    """
    version, sha256 = entry["version"], entry["sha256"]
    cache = _digests(hass)
    now = dt_util.utcnow()
    cached = cache.get((version, sha256))
    if cached is not None and now - cached[1] < PLUGIN_DIGEST_CACHE:
        verdict = cached[0]
    else:
        verdict = await _async_ask_github(hass, entry)
        cache[(version, sha256)] = (verdict, now)
    if verdict == DIGEST_MISMATCH:
        raise PackageError(
            DIGEST,
            f"GitHub's digest of the {version} release asset is not the sha256 the signed "
            "index names",
            {"version": version},
        )
    return verdict


async def _async_ask_github(hass: HomeAssistant, entry: dict[str, Any]) -> str:
    version = entry["version"]
    url = PLUGIN_RELEASE_API + version
    try:
        _, body, _ = await _async_get(
            async_get_clientsession(hass),
            url,
            PLUGIN_RELEASE_API_LIMIT,
            {"Accept": "application/vnd.github+json"},
        )
    except (_Fetched, TimeoutError, aiohttp.ClientError) as error:
        _LOGGER.info(
            "The release asset digest of plugin %s could not be read from GitHub (%s); "
            "going on with the signed checksum alone",
            version,
            error,
        )
        return DIGEST_UNAVAILABLE
    digest = (
        _asset_digest(body, entry["filename"]) if len(body) <= PLUGIN_RELEASE_API_LIMIT else None
    )
    if digest is None:
        _LOGGER.info(
            "GitHub reports no digest for the release asset of plugin %s; going on with the "
            "signed checksum alone",
            version,
        )
        return DIGEST_UNAVAILABLE
    if digest != f"sha256:{entry['sha256']}":
        _LOGGER.warning(
            "GitHub's digest of the release asset of plugin %s is %s, and the signed release "
            "index names sha256:%s; the package is refused",
            version,
            digest,
            entry["sha256"],
        )
        return DIGEST_MISMATCH
    return DIGEST_MATCH


def _bundle_bytes(entry: dict[str, Any]) -> bytes | None:
    """The bundled package's bytes when they are the signed entry's, else None."""
    try:
        bundled: BundledPlugin = bundle_module.load_bundled_plugin()
    except (BundleError, OSError):
        return None
    if bundled.version != entry["version"] or bundled.sha256 != entry["sha256"]:
        return None
    try:
        data = Path(bundled.path).read_bytes()
    except OSError:
        return None
    return data


async def _async_download(hass: HomeAssistant, entry: dict[str, Any]) -> bytes:
    version = entry["version"]
    url = PLUGIN_INDEX_ORIGIN + entry["filename"]
    placeholders = {"version": version}
    try:
        # `_async_get` holds the request to the check's own ten seconds.
        _, data, _ = await _async_get(
            async_get_clientsession(hass),
            url,
            min(entry["size"], release_index.MAX_PACKAGE_BYTES),
            {},
        )
    except _Fetched as error:
        raise PackageError(DOWNLOAD, str(error), placeholders) from error
    except (TimeoutError, aiohttp.ClientError) as error:
        raise PackageError(
            DOWNLOAD, f"{url} could not be fetched: {error!r}", placeholders
        ) from error
    return data


async def async_fetch_release(hass: HomeAssistant, version: str) -> PackageSource:
    """The verified package of `version`, from the bundle when it is those bytes, else fetched.

    Raises PackageError before anything else happens when the version cannot be had.
    """
    cache = async_release_index_cache(hass)
    await cache.async_load()
    try:
        entry = eligible_release(
            cache.index, version, integration_version=cache.integration_version
        )
        data = await hass.async_add_executor_job(_bundle_bytes, entry)
        origin = ORIGIN_BUNDLE
        if data is None:
            # Asked before the download: a package GitHub disagrees about is not worth
            # fetching.
            await async_digest_check(hass, entry)
            data = await _async_download(hass, entry)
            origin = ORIGIN_DOWNLOAD
        await hass.async_add_executor_job(verify_package, data, entry)
    except PackageError as error:
        _LOGGER.warning(
            "Plugin %s was not fetched (%s): %s. Nothing was sent to any receiver",
            version,
            error.code,
            error.detail,
        )
        raise
    _LOGGER.info(
        "Plugin %s verified against the signed release index (%s, %d bytes)",
        version,
        "the bundled package" if origin == ORIGIN_BUNDLE else "downloaded",
        len(data),
    )
    return PackageSource(
        version=version,
        data=data,
        sha256=entry["sha256"],
        commit=entry["commit"],
        depends=tuple(entry["depends"]),
        origin=origin,
        self_update=bool(entry["self_update"]),
    )
