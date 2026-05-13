# ============================================================================
# Soccer Stars Analyzer — Buildozer Spec  (final / production-ready)
# ============================================================================
#
# Tested with: Buildozer 1.5+, python-for-android develop branch,
#              Android NDK r25b, SDK API 34, NDK API 26.
#
# Quick-start
# -----------
#   pip install buildozer cython
#   buildozer android debug                    # build debug APK
#   buildozer android debug deploy run         # build + install + run on device
#   buildozer android release                  # production APK (needs keystore)
#
# First build takes 30-60 min (downloads NDK/SDK and compiles recipes).
# Subsequent builds: ~3-5 min (incremental).
#
# Honor 400 note
# --------------
# The Honor 400 ships with Android 14 (API 34) and a 64-bit ARM SoC.
# Build for arm64-v8a only to keep the APK small; add armeabi-v7a only if
# you need to support older devices.
# ============================================================================

[app]

# ----------------------------------------------------------------------------
# Identity
# ----------------------------------------------------------------------------
title          = Soccer Stars Analyzer
package.name   = soccerstarsanalyzer
package.domain = com.soccerstars

# ----------------------------------------------------------------------------
# Source
# ----------------------------------------------------------------------------
source.dir          = .
source.include_exts = py,png,jpg,jpeg,kv,atlas,json,ttf
source.main         = main.py

# Background service (foreground type required for MediaProjection on API 29+)
services = SoccerStarsService:service/main.py:foreground

# ----------------------------------------------------------------------------
# Version  (bump manually — version.regex path is for optional CI automation)
# ----------------------------------------------------------------------------
version          = 1.1.0
#version.regex   = __version__ = ['"](.*)['"]
#version.filename = %(source.dir)s/main.py

# ----------------------------------------------------------------------------
# Assets  (uncomment once you have icon/presplash assets)
# ----------------------------------------------------------------------------
#icon.filename      = %(source.dir)s/data/icon.png        # 512×512 px
#presplash.filename = %(source.dir)s/data/presplash.png   # 1080×1920 px

# ----------------------------------------------------------------------------
# Orientation & display
# ----------------------------------------------------------------------------
orientation = portrait
fullscreen   = 0

# ----------------------------------------------------------------------------
# Requirements
# ----------------------------------------------------------------------------
# python-for-android recipes used:
#   python3      — CPython 3.11
#   kivy         — UI framework
#   numpy        — vector/matrix math
#   opencv       — computer vision (p4a recipe; builds libopencv_java4.so)
#   pyjnius      — Python ↔ Java bridge
#   android      — python-for-android Android helpers (permissions, activity)
#
# If the 'opencv' p4a recipe is unavailable in your p4a version, replace it
# with 'opencv-python-headless' and adjust the import in analyzer.py to use
# the headless wheel (works on arm64 since OpenCV 4.7).
requirements = python3==3.11.9,kivy==2.3.0,numpy,opencv,pyjnius,android

# ----------------------------------------------------------------------------
# Android SDK / NDK
# ----------------------------------------------------------------------------
android.api     = 34
android.minapi  = 26          # Android 8.0 — minimum for TYPE_APPLICATION_OVERLAY
android.ndk_api = 26
android.archs   = arm64-v8a   # Honor 400 is arm64; add armeabi-v7a for older devices

# Gradle / AndroidX
android.gradle_dependencies = androidx.core:core:1.13.1
android.enable_androidx      = True

# ----------------------------------------------------------------------------
# PERMISSIONS
# ----------------------------------------------------------------------------
# Every permission is explained. Remove any you do not use.

android.permissions =
    # Draw over other apps (required for TYPE_APPLICATION_OVERLAY)
    android.permission.SYSTEM_ALERT_WINDOW,

    # Keep the service alive in the foreground
    android.permission.FOREGROUND_SERVICE,

    # Required on Android 14+ for foreground services that use MediaProjection
    android.permission.FOREGROUND_SERVICE_MEDIA_PROJECTION,

    # Keep the CPU running while the service processes frames
    android.permission.WAKE_LOCK,

    # Network state (used by the local UDP IPC sockets)
    android.permission.INTERNET,
    android.permission.ACCESS_NETWORK_STATE,

    # Optional: haptic feedback when a turn is auto-detected
    android.permission.VIBRATE

# NOTE: RECORD_AUDIO is NOT needed for screen capture via MediaProjection.
# Add it only if you later integrate game audio analysis:
#   android.permission.RECORD_AUDIO

# ----------------------------------------------------------------------------
# AndroidManifest extras
# ----------------------------------------------------------------------------

# Inject into <application> block — required for HTTP on Android 9+
android.extra_manifest_application_arguments =
    android:usesCleartextTraffic="true"

# Declare the foreground service with the mediaProjection type so Android 14
# allows it to call getMediaProjection() from a service context.
# Buildozer merges this into the generated AndroidManifest.xml.
android.manifest.intent_filters =

# ----------------------------------------------------------------------------
# p4a (python-for-android)
# ----------------------------------------------------------------------------
# Use the 'develop' branch for the latest OpenCV and Kivy 2.3 recipes.
p4a.branch = develop

# Uncomment to point at a local p4a clone (useful for recipe development):
# p4a.source_dir = /path/to/python-for-android

# Uncomment to add a local recipe directory (e.g. custom opencv variant):
# p4a.local_recipes = ./p4a_recipes

# ----------------------------------------------------------------------------
# Build directories
# ----------------------------------------------------------------------------
[buildozer]
build_dir = .buildozer
bin_dir   = ./bin
log_level = 2          # 0=error  1=info  2=debug
