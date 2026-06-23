import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def read_project_file(*parts):
    return (ROOT.joinpath(*parts)).read_text(encoding="utf-8")


class I18nTemplateTestCase(unittest.TestCase):
    def test_global_language_script_prefers_saved_then_browser_language(self):
        script = read_project_file("supysonic", "static", "js", "supysonic.js")

        self.assertIn("function getInitialLanguage()", script)
        self.assertIn("getStoredPreference('language')", script)
        self.assertIn("function getBrowserLanguage()", script)
        self.assertIn("navigator.languages", script)
        self.assertIn("startsWith('zh')", script)
        self.assertIn("setStoredPreference('language', selectedLanguage)", script)

    def test_global_language_script_updates_text_attrs_and_document_title(self):
        script = read_project_file("supysonic", "static", "js", "supysonic.js")

        self.assertIn("function i18nText(enText, zhText)", script)
        self.assertIn("data-i18n-attr", script)
        self.assertIn(".split(',')", script)
        self.assertIn('meta[name="i18n-title"]', script)
        self.assertIn("document.title = titleText", script)
        self.assertIn("document.documentElement.lang", script)

    def test_layout_exposes_early_language_helpers_for_inline_page_scripts(self):
        template = read_project_file("supysonic", "templates", "layout.html")

        self.assertIn('meta name="i18n-title"', template)
        self.assertIn("block document_title_en", template)
        self.assertIn("block document_title_zh", template)
        self.assertIn("window.getLanguage = function", template)
        self.assertIn("window.i18nText = function", template)
        self.assertIn('data-i18n-attr="aria-label"', template)
        self.assertIn('data-language-toggle="zh"', template)

    def test_main_console_templates_have_i18n_copy(self):
        templates = {
            "adduser.html": ("新增用户", "data-i18n-attr=\"placeholder\""),
            "addfolder.html": ("新增文件夹", "媒体根目录"),
            "admin-tasks.html": ("后台任务", 'data-i18n-en="just now"'),
            "control.html": ("控制台", "i18nText('No player selected.'"),
            "devices.html": ("设备", 'i18nText("No devices connected."'),
            "shares.html": ("分享", "setCopyButtonLabel('Copied'"),
            "metadata-workspace.html": ("元数据", "data-i18n-en=\"Inbox\""),
            "metadata-review-task.html": ("专辑审核任务", "Confirm this review task"),
        }

        for filename, expected_fragments in templates.items():
            with self.subTest(template=filename):
                template = read_project_file("supysonic", "templates", filename)
                self.assertIn("data-i18n", template)
                for fragment in expected_fragments:
                    self.assertIn(fragment, template)

    def test_metadata_partials_cover_search_and_editor_copy(self):
        artist_template = read_project_file(
            "supysonic",
            "templates",
            "partials",
            "metadata-artist-content.html",
        )
        album_template = read_project_file(
            "supysonic",
            "templates",
            "partials",
            "metadata-album-content.html",
        )

        self.assertIn('data-i18n-en="Search artists"', artist_template)
        self.assertIn('data-i18n-zh="搜索艺人"', artist_template)
        self.assertIn("metadataArtistI18nText('No matching artists found.'", artist_template)
        self.assertIn("window.setTimeout(filterArtistCards, 0)", artist_template)

        self.assertIn('data-i18n-en="Search albums"', album_template)
        self.assertIn('data-i18n-zh="搜索专辑"', album_template)
        self.assertIn("metadataAlbumI18nText('No matching albums found.'", album_template)
        self.assertIn("window.setTimeout(filterAlbumCards, 0)", album_template)

    def test_dynamic_share_buttons_keep_i18n_labels(self):
        playlist_template = read_project_file("supysonic", "templates", "playlist.html")
        shares_template = read_project_file("supysonic", "templates", "shares.html")

        self.assertIn("function setConfirmShareLinkLabel", playlist_template)
        self.assertNotIn("this.textContent = i18nText('Creating...'", playlist_template)
        self.assertIn("function setCopyButtonLabel", shares_template)
        self.assertNotIn("button.textContent = i18nText('Copied'", shares_template)


if __name__ == "__main__":
    unittest.main()
