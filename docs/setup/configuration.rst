Configuration
=============

Supysonic looks for four files for its configuration: :file:`/etc/supysonic`,
:file:`~/.supysonic`, :file:`~/.config/supysonic/supysonic.conf` and
:file:`supysonic.conf` in the current working directory, in this order, merging
values from all files.

Configuration files must respect a structure similar to Windows INI file, with
``[section]`` headers and using a ``KEY = VALUE`` or ``KEY: VALUE`` syntax.

If you cloned Supysonic from its `GitHub repository`__ you'll find a roughly
documented configuration sample file at the root of the project, file
conveniently named :file:`config.sample`. More details below.

__ https://github.com/spl0k/supysonic

``[base]`` section
------------------

This sections defines the database and additional scanning config.

``database_uri``
   The most important configuration, defines the type and
   parameters of the database Supysonic should connect to. It usually includes
   username, password, hostname and database name. The typical form of a
   database URI is::

      driver://username:password@host:port/database

   If the connection needs some additional parameters, they can be provided as a
   query string, such as::

      driver://username:password@host:port/database?param1=value1&param2=value2

   Supported drivers are ``sqlite``, ``mysql`` and ``postgres`` (or
   ``postgresql``).

   As SQLite connects to local files, the format is slightly different. The
   "file" portion of the URI is the filename of the database. For a relative
   path, it requires three slashes, for absolute paths it's also three slashes
   followed by the absolute path, meaning actually four slashes on Unix systems.

   .. highlight:: ini

   ::

      ; Relative path
      database_uri = sqlite:///relative-file.db
      ; Absolute path on Unix-based systems
      database_uri = sqlite:////home/user/supysonic.db
      ; Absolute path on Windows
      database_uri = sqlite:///C:\Users\user\supysonic.db

   A MySQL-compatible database requires either ``MySQLdb`` or ``pymysql`` to be
   installed. PostgreSQL needs ``psycopg2``.

   .. note::

      For MySQL if no character set is defined on the URI it defaults to
      ``utf8mb4`` regardless of what's set on your MySQL installation.

   If ``database_uri`` isn't provided, it defaults to a SQLite database stored
   in :file:`/tmp/supysonic/supysonic.db`.

``scanner_extensions``
   A space separated list of file extensions the scanner is restricted to.
   Useful if you have multiple audio formats in your library but only want to
   serve some. If left empty, the scanner will try to read every file it finds.

``follow_symlinks``
   If set to ``yes``, allows the scanner to follow symbolic links.

   Disabled by default, enable it only if you trust your file system as nothing
   is done to handle broken links or loops.

Sample configuration::

   [base]
   ; A database URI. Default: sqlite:////tmp/supysonic/supysonic.db
   database_uri = sqlite:////var/supysonic/supysonic.db
   ;database_uri = mysql://supysonic:supysonic@localhost/supysonic
   ;database_uri = postgres://supysonic:supysonic@localhost/supysonic

   ; Optional, restrict scanner to these extensions. Default: none
   scanner_extensions = mp3 ogg

   ; Should the scanner follow symbolic links? Default: no
   follow_symlinks = no

``[webapp]`` section
--------------------

Configuration relative to the HTTP server.

``cache_dir``
   Directory used to store generated files, such as resized cover art or
   transcoded files. Defaults to :file:`/tmp/supysonic`.

``cache_size``
   Maximum size (in megabytes) of the cache (except for trancodes).
   Defaults to 512 MB.

``transcode_cache_size``
   Maximum size (in megabytes) of the transcode cache.
   Defaults to 1024 MB (1 GB).

``log_dir``
   Optional directory for structured web logs. When set, Supysonic writes
   :file:`supysonic.log`, :file:`access.log`, :file:`stream.log`, :file:`task.log`,
   :file:`emo.log`, :file:`scanner.log`, :file:`api.log`, and
   :file:`metadata.log` there. If ``log_level`` is ``DEBUG``, it also writes
   :file:`web.debug.log`.

``log_file``
   Legacy compatibility setting. If ``log_dir`` is empty and ``log_file`` is
   set, Supysonic uses the parent directory of ``log_file`` for the structured
   web log files listed above. Leave both empty to disable file logging.

``log_backup_count``
   Number of rotated log files to keep for each managed log file.
   Defaults to ``7``.

``log_level``
   Defines the minimum severity threshold of messages to be added to
   the managed web log files. Possible values are:

   * ``DEBUG``
   * ``INFO``
   * ``WARNING``
   * ``ERROR``
   * ``CRITICAL``

   Defaults to ``WARNING``.

``log_rotate``
   Enable automatic log rotation (when logs are enabled) every day at midnight.
   Set it to ``no`` if you don't want to rotate the logs or if you use external
   utilities such as :command:`logrotate`. Defaults to ``yes``.

``mount_api`` (``on`` or ``off``)
   Enable or disable the Subsonic REST API. Should be kept on or Supysonic would
   be quite useless. Exists mostly for testing purposes.
   Defaults to ``on``.

``mount_webui`` (``on`` or ``off``)
   Enable or disable the administrative web interface.

   .. note::
      Setting this off will prevent users from defining a preferred transcoding
      format.

   Defaults to ``on``.

``emo_web_realtime_protocol``
   Selects the realtime client rendered by both :file:`/player` and
   :file:`/control`. The only accepted values are ``legacy`` and ``strict_v2``.
   Invalid values fail back to the explicit legacy page; a strict-v2 protocol
   failure never causes an automatic runtime downgrade. Defaults to ``legacy``.

``emo_strict_v2_allow_local_test_evidence``
   Allows a development deployment to use packaged ``local-test-only:``
   conformance evidence for external client integration testing. This option
   is effective only when ``emo_development_mode`` is also enabled. Both
   settings must remain disabled in production. Defaults to ``off``.

``emo_browser_otp_ttl_seconds``
   Lifetime of the one-time, same-origin browser password used by the web
   player and control console for Socket.IO ``auth.login``. The credential is
   bound to the authenticated Cookie session and is not stored in browser
   persistent storage. Defaults to ``60`` seconds.

``emo_browser_otp_issues_per_session_per_minute``
   Maximum browser passwords issued to one authenticated browser session per
   minute. Defaults to ``12``.

``emo_browser_otp_outstanding_per_session``
   Maximum unconsumed browser passwords retained for one authenticated Cookie
   session. This permits player and control tabs to bootstrap concurrently;
   issuing beyond the limit evicts the oldest password. Defaults to ``4``.

``emo_browser_otp_global_capacity``
   Maximum total number of unconsumed browser passwords in the in-memory store.
   It also bounds the number of per-session issuance windows retained by the
   rate limiter, preventing consumed credentials from bypassing the store's
   memory ceiling. New credentials receive a temporary-unavailable response
   when either limit is reached. Defaults to ``10000``.

``emo_web_strict_v2_follow_enabled``
   Advertise Follow capability from the strict-v2 web player.

``emo_web_strict_v2_handoff_enabled``
   Advertise Handoff prepare and effective-at capabilities from the strict-v2
   web player.

``emo_web_strict_v2_broadcast_enabled``
   Advertise Broadcast capability from the strict-v2 web player and control
   console.

   All three optional-profile gates default to ``off`` and should be enabled
   only after their browser acceptance evidence and server profile readiness
   have been reviewed. They do not override server-side deployment or
   conformance readiness.

``index_ignored_prefixes``
   Space-separated list of prefixes that should be ignored from artist names
   when returning their index. Example: if the word *The* is in this list,
   artist *The Rolling Stones* will be listed under the letter *R*. The match is
   case insensitive.
   Defaults to ``El La Le Las Les Los The``.

``online_lyrics``
   If enabled, will fetch the lyrics (when requested) from ChartLyrics if they
   aren't available locally (either from metadata or from text files).
   Defaults to ``no``.

Sample configuration::

   [webapp]
   ; Optional cache directory. Default: /tmp/supysonic
   cache_dir = /var/supysonic/cache

   ; Main cache max size in MB. Default: 512
   cache_size = 512

   ; Transcode cache max size in MB. Default: 1024 (1GB)
   transcode_cache_size = 1024

   ; Optional log directory for structured web logs. When set, Supysonic writes
   ; supysonic.log, access.log, stream.log, task.log, emo.log, scanner.log,
   ; api.log and metadata.log here. At DEBUG level it also writes web.debug.log.
   ; Default: none
   log_dir = /log/web

   ; Number of rotated log files to keep. Default: 7
   log_backup_count = 7

   ; Log level. Possible values: DEBUG, INFO, WARNING, ERROR, CRITICAL.
   ; Default: WARNING
   log_level = WARNING

   ; Enable log rotation. Default: yes
   log_rotate = yes

   ; Enable the Subsonic REST API. You'll most likely want to keep this on.
   ; Here for testing purposes. Default: on
   ;mount_api = on

   ; Enable the administrative web interface. Default: on
   ;mount_webui = on

   ; Render both /player and /control with legacy or strict-v2 realtime logic.
   ;emo_web_realtime_protocol = legacy

   ; Development-only gate for local-test-only conformance evidence.
   ; Requires emo_development_mode = on. Never enable in production.
   ;emo_strict_v2_allow_local_test_evidence = off

   ; One-time browser Socket.IO password lifetime. Default: 60 seconds.
   ;emo_browser_otp_ttl_seconds = 60

   ; Browser password issuance limits.
   ;emo_browser_otp_issues_per_session_per_minute = 12
   ;emo_browser_otp_outstanding_per_session = 4
   ;emo_browser_otp_global_capacity = 10000

   ; Optional web profiles default to off pending browser acceptance evidence.
   ;emo_web_strict_v2_follow_enabled = off
   ;emo_web_strict_v2_handoff_enabled = off
   ;emo_web_strict_v2_broadcast_enabled = off

   ; Space separated list of prefixes that should be ignored on index endpoints
   ; Default: El La Le Las Les Los The
   index_ignored_prefixes = El La Le Las Les Los The

   ; Enable the ChartLyrics API. Default: off
   online_lyrics = off

.. _conf-daemon:

``[daemon]`` section
--------------------

Configuration for the daemon process that is used to watch for changes in the
library folders and providing the jukebox feature.

``socket``
   Unix domain socket file (or named pipe on Windows) used to communicate
   between the daemon and clients that rely on it (eg. CLI, folder admin web
   page, etc.). Note that using an IP address here isn't supported.
   Default: :file:`/tmp/supysonic/supysonic.sock`

``run_watcher``
   Whether or not to start the watcher that will listen for library changes.
   Default: yes

``wait_delay``
   Delay (in seconds) before triggering the scanning operation after a change
   have been detected. This prevents running too many scans when multiple
   changes are detected for a single file over a short time span.
   Default: 5 seconds.

``jukebox_command``
   Command used by the jukebox mode to play a single file.
   See the :doc:`jukebox documentation <../jukebox>` for more details.

``log_dir``
   Optional directory for daemon logs. When set, Supysonic writes
   :file:`supysonic.log`, :file:`daemon.log`, :file:`watcher.log`, and
   :file:`scanner.log` there. If ``log_level`` is ``DEBUG``, it also writes
   :file:`daemon.debug.log` in the same directory.

``log_file``
   Legacy compatibility setting. Used only when ``log_dir`` is empty.
   If left empty, logging is sent to stderr.

``log_backup_count``
   Number of rotated log files to keep for each managed log file.
   Defaults to ``7``.

``log_level``
   Defines the minimum severity threshold of messages to be added to
   the managed daemon log files. Possible values are:

   * ``DEBUG``
   * ``INFO``
   * ``WARNING``
   * ``ERROR``
   * ``CRITICAL``

   Defaults to ``WARNING``.

``log_rotate``
   Enable automatic log rotation (when logs are enabled) every day at midnight.
   Set it to ``no`` if you don't want to rotate the logs or if you use external
   utilities such as :command:`logrotate`. Defaults to ``yes``.

``recommend_daily_refresh``
   Whether the daemon should create recommended playlists automatically.
   Defaults to ``yes``.

``recommend_refresh_interval``
   Poll interval, in seconds, for the daily recommendation refresh check.
   Defaults to ``300``.

``recommend_playlist_size``
   Number of tracks to include in each generated recommended playlist.
   Defaults to ``50``.

``recommend_playlist_archive_enabled``
   Whether recommended playlists older than the retention window should be
   archived to JSON and removed from the playlist table during the recommendation
   creation task. Defaults to ``yes``.

``recommend_playlist_retention_days``
   Number of recommendation days to keep in the playlist table. Older
   recommended playlists are archived under
   :file:`<webapp.cache_dir>/recommend-playlists/<user>/`. Defaults to ``5``.

``mood_scene_playlists_daily_refresh``
   Whether the daemon should create or update system mood/scene playlists
   automatically each day. This uses existing semantic metadata and does not
   call an LLM. Defaults to ``yes``.

``mood_scene_playlists_refresh_interval``
   Poll interval, in seconds, for the daily mood/scene playlist refresh check.
   The scheduler still refreshes at most once per natural day. Defaults to
   ``300``.

``mood_scene_playlist_size``
   Number of tracks to include in each generated mood/scene playlist. Defaults
   to ``30``.

``mood_scene_playlist_retention_days``
   Number of system mood/scene playlist days to keep in the playlist table.
   Older system mood/scene playlists are removed during the refresh task.
   Defaults to ``1``.

``mood_scene_playlists_active_users_only``
   Whether automatic mood/scene playlist generation should be limited to users
   with play activity or a last-play track. Defaults to ``yes``.

``track_metadata_enrichment``
   Whether the daemon should enrich track recommendation metadata automatically.
   Defaults to ``no`` so upgrades never trigger external metadata work by
   surprise.

``track_metadata_enrichment_provider``
   Provider used by daemon-driven track metadata enrichment. ``local`` uses only
   existing library metadata and does not call external services. ``llm`` uses
   the OpenAI-compatible endpoint, API key, model, timeout, token limit and
   temperature from the ``[recommendation_agent]`` section. Defaults to
   ``local``.

``track_metadata_enrichment_interval``
   Poll interval, in seconds, for daemon-driven track metadata enrichment.
   Defaults to ``300``.

``track_metadata_enrichment_batch_size``
   Maximum tracks to enrich in one daemon pass. Defaults to ``10``.

``track_metadata_enrichment_stale_lock_seconds``
   Seconds before a running enrichment task is considered stale and can be
   retried. Defaults to ``900``.

``track_metadata_enrichment_send_path_hints``
   Whether enrichment providers may receive full local path hints. Defaults to
   ``no``. Keep this disabled unless the configured provider is trusted.

``track_metadata_enrichment_log_payload``
   Whether successful enrichment logs should include generated mood, scene,
   tag and summary fields. Defaults to ``no`` because it can be noisy and may
   expose generated semantic text in logs.

Sample configuration::

   [daemon]
   ; Socket file the daemon will listen on for incoming management commands
   ; Default: /tmp/supysonic/supysonic.sock
   socket = /var/run/supysonic.sock
   ; Syntax for windows named pipe:
   ;socket = \\.\pipe\supysonic.sock

   ; Defines if the file watcher should be started. Default: yes
   run_watcher = yes

   ; Delay in seconds before triggering scanning operation after a change have been
   ; detected.
   ; This prevents running too many scans when multiple changes are detected for a
   ; single file over a short time span. Default: 5
   wait_delay = 5

   ; Command used by the jukebox
   jukebox_command = mplayer -ss %offset %path

   ; Optional log directory for daemon logs. When set, Supysonic writes
   ; supysonic.log, daemon.log, watcher.log, scanner.log and, at DEBUG level,
   ; daemon.debug.log here. Default: none
   log_dir = /log/daemon

   ; Number of rotated log files to keep. Default: 7
   log_backup_count = 7
   log_level = INFO

   ; Enable log rotation. Default: yes
   log_rotate = yes

   ; Create recommended playlists automatically each day. Default: yes
   recommend_daily_refresh = yes

   ; Poll interval in seconds for the daily recommendation refresh check.
   ; Default: 300
   recommend_refresh_interval = 300

   ; Number of tracks to include in each generated recommended playlist.
   ; Default: 50
   recommend_playlist_size = 50

   ; Archive recommended playlists older than the retention window into JSON
   ; files under <webapp.cache_dir>/recommend-playlists/<user>/. Default: yes
   recommend_playlist_archive_enabled = yes

   ; Keep only this many recommendation days in the playlist table. Older
   ; ones are archived to JSON when the recommendation creation task runs.
   ; Default: 5
   recommend_playlist_retention_days = 5

   ; Create or update system mood/scene playlists automatically each day.
   ; Default: yes
   mood_scene_playlists_daily_refresh = yes

   ; Poll interval in seconds for the daily mood/scene playlist refresh check.
   ; Default: 300
   mood_scene_playlists_refresh_interval = 300

   ; Number of tracks in each generated mood/scene playlist. Default: 30
   mood_scene_playlist_size = 30

   ; Keep only this many system mood/scene playlist days. Default: 1
   mood_scene_playlist_retention_days = 1

   ; Generate automatically only for active users. Default: yes
   mood_scene_playlists_active_users_only = yes

   ; Enrich track recommendation metadata automatically from the daemon.
   ; Default: no
   track_metadata_enrichment = no

   ; Provider for daemon-driven track metadata enrichment. "llm" reuses
   ; [recommendation_agent] endpoint/key/model settings. Default: local
   track_metadata_enrichment_provider = local

   ; Poll interval in seconds for track metadata enrichment. Default: 300
   track_metadata_enrichment_interval = 300

   ; Maximum tracks enriched per daemon pass. Default: 10
   track_metadata_enrichment_batch_size = 10

   ; Seconds before a running enrichment task is considered stale. Default: 900
   track_metadata_enrichment_stale_lock_seconds = 900

   ; Allow full path hints to be sent to an enrichment provider. Default: no
   track_metadata_enrichment_send_path_hints = no

   ; Log generated mood/scene/tag/summary fields after each enriched track.
   ; Default: no
   track_metadata_enrichment_log_payload = no

.. _conf-musicbrainz:

``[musicbrainz]`` section
-------------------------

This section controls MusicBrainz requests used by album metadata enrichment.
MusicBrainz is the default structured source for release dates, years, release
types, and MusicBrainz identifiers.

Album enrichment only fills missing album and track metadata. It does not
overwrite existing values, and automatic writes may create or update an album
review task with reason ``external_enrichment`` for human audit.

``api_url``
   MusicBrainz API root URL. Defaults to ``https://musicbrainz.org/ws/2``.

``cover_art_api_url``
   Cover Art Archive root URL. Defaults to ``https://coverartarchive.org``.

``user_agent``
   User-Agent sent to MusicBrainz. Production deployments should use a
   meaningful value with contact information.

``request_delay_seconds``
   Delay between MusicBrainz enrichment requests, in seconds. Defaults to
   ``1.0``.

Sample configuration::

   [musicbrainz]
   ;api_url = https://musicbrainz.org/ws/2
   ;cover_art_api_url = https://coverartarchive.org
   ;user_agent = Supysonic/1.0
   ;request_delay_seconds = 1.0

.. _conf-discogs:

``[discogs]`` section
---------------------

This section controls optional Discogs album metadata enrichment. Discogs is
disabled unless ``enabled`` is on and ``token`` is non-empty. Missing credentials
or API failures skip Discogs without blocking MusicBrainz enrichment. HTTP
``429`` rate limits, request timeouts, and network failures are logged and
skipped without aborting the scan.

``enabled``
   Enable Discogs enrichment. Defaults to ``off``.

``api_url``
   Discogs API root URL. Defaults to ``https://api.discogs.com``.

``token``
   Discogs personal access token. Do not commit real tokens.

``user_agent``
   User-Agent sent to Discogs. Production deployments should use a meaningful
   value with contact information.

``request_delay_seconds``
   Delay between Discogs enrichment requests, in seconds. Defaults to ``1.0``.

Sample configuration::

   [discogs]
   enabled = off
   ;api_url = https://api.discogs.com
   ;token =
   ;user_agent = Supysonic/1.0
   ;request_delay_seconds = 1.0

.. _conf-lastfm:

``[lastfm]`` section
--------------------

This section allow defining API keys to enable Last.FM integration in
Supysonic. Currently it is only used to *scrobble* played tracks and update
the *now playing* information.

See https://www.last.fm/api to obtain such keys.

Once keys are set, users have to link their account by visiting their profile
page on Supysonic's administrative UI.

``api_key``
   Last.FM API key

``secret``
   secret key associated to the API key

Sample configuration::

   [lastfm]
   ; API and secret key to enable scrobbling. http://www.last.fm/api/accounts
   ; Defaults: none
   ;api_key =
   ;secret =

.. _conf-listenbrainz:

``[listenbrainz]`` section
--------------------------

This section allows a custom ListenBrainz instance to be configured
for scrobbling. ListenBrainz is a music scrobbling service with social
features, similar to LastFM, but it is open source and
self-hostable. Supysonic can configured with any ListenBrainz
instance, but it connects to the official instance by default.

In order to connect to ListenBrainz, each user requires an user token
that can be obtained from their ListenBrainz profile (more information
in the API docs). This token has to be configured per profile using
the web UI.

The ListenBrainz API documentation can be found here:
https://listenbrainz.readthedocs.io/en/latest/users/api/index.html

``api_url``
   root URL of the ListenBrainz API for the instance

Sample configuration::

   [listenbrainz]
   ; root URL of the ListenBrainz API.
   ; Defaults: https://api.listenbrainz.org/
   ;api_url =

.. _conf-transcoding:

``[transcoding]`` section
-------------------------

This section defines command-line programs to be used to convert an audio file
to another format or change its bitrate. All configurations in the sample below
have **not** been thoroughly tested.
For more details, please refer to the
:doc:`transcoding configuration <../transcoding>`.

::

   [transcoding]
   ; Programs used to convert from one format/bitrate to another. Defaults: none
   transcoder_mp3_mp3 = lame --quiet --mp3input -b %outrate %srcpath -
   transcoder = ffmpeg -i %srcpath -ab %outratek -v 0 -f %outfmt -
   decoder_mp3 = mpg123 --quiet -w - %srcpath
   decoder_ogg = oggdec -Q -o - %srcpath
   decoder_flac = flac -d -c -s %srcpath
   encoder_mp3 = lame --quiet -b %outrate - -
   encoder_ogg = oggenc2 -q -M %outrate -

``[mimetypes]`` section
-----------------------

Use this section if the system Supysonic is installed on has trouble guessing
the mimetype of some files. This might only be useful in some rare cases.

See the following links for a list of examples:

* https://en.wikipedia.org/wiki/Media_type#Common_examples
* https://www.iana.org/assignments/media-types/media-types.xhtml

::

   [mimetypes]
   ; Extension to mimetype mappings in case your system has some trouble guessing
   ; Default: none
   ;mp3 = audio/mpeg
   ;ogg = audio/vorbis
