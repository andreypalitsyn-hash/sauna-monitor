[app]

title = Sauna Monitor
package.name = saunamonitor
package.domain = org.example
source.dir = .
source.include_exts = py,png,jpg,kv,ttf
version = 0.1
requirements = python3,kivy
orientation = landscape
osx.python_version = 3
osx.kivy_version = 2.3.1
android.permissions = INTERNET
android.api = 30
android.minapi = 21
android.graphics_backend = sdl2
android.arch = arm64-v8a, armeabi-v7a
android.ndk = 23b
android.sdk = 30
android.ant_platform = 30
android.allow_backup = True
android.debuggable = True
android.logcat_filters = *:S python:D
log_level = 2

[buildozer]
log_level = 2
