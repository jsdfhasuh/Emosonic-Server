ALTER TABLE client_prefs ADD FOREIGN KEY (user_id) REFERENCES user(id);
CREATE INDEX index_client_prefs_user_id_fk ON client_prefs(user_id);
