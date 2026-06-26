CREATE TABLE IF NOT EXISTS music_request (
    id CHAR(32) PRIMARY KEY,
    user_id CHAR(32) NOT NULL REFERENCES user(id),
    artist_name VARCHAR(256) NOT NULL,
    album_name VARCHAR(256),
    tracks_json TEXT,
    note TEXT,
    status VARCHAR(32) NOT NULL,
    status_note TEXT,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    resolved_at DATETIME
) DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE INDEX index_music_request_status_created_at ON music_request(status, created_at);
CREATE INDEX index_music_request_user_created_at ON music_request(user_id, created_at);
CREATE INDEX index_music_request_artist_album_status ON music_request(artist_name, album_name, status);
