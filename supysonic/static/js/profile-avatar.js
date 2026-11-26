(() => {
  const portrait = document.querySelector("[data-daily-avatar]");
  if (portrait) {
    const url = portrait.dataset.dailyAvatar;
    let version = "";
    let checking = false;
    const refresh = async () => {
      if (document.hidden || checking) return;
      checking = true;
      try {
        const response = await fetch(url, {method: "HEAD", cache: "no-store",
          headers: version ? {"If-None-Match": version} : {}});
        if (response.status === 404) {
          portrait.textContent = portrait.dataset.initial;
          version = "";
        } else if (response.ok && response.headers.get("Content-Type") === "image/png") {
          const next = response.headers.get("ETag");
          if (next && next !== version) {
            const img = new Image(56, 56);
            img.alt = "";
            img.src = url + "?v=" + encodeURIComponent(next);
            await img.decode();
            portrait.replaceChildren(img);
            version = next;
          }
        }
      } catch {
        // Keep showing the current portrait until the connection returns.
      } finally {
        checking = false;
      }
    };
    refresh();
    window.setInterval(refresh, 60000);
    document.addEventListener("visibilitychange", refresh);
  }
  const editor = document.querySelector(".listening-avatar-generator");
  if (!editor) return;
  const generate = editor.querySelector("[data-avatar-generate]");
  const apply = editor.querySelector("[data-avatar-apply]");
  const status = editor.querySelector("[data-avatar-status]");
  const preview = apply.querySelector("img");
  const candidate = apply.elements.avatar;
  const button = generate.querySelector("button");
  let objectUrl = null;

  function say(english, chinese) {
    status.textContent = document.body.dataset.language === "zh" ? chinese : english;
  }

  function discard() {
    apply.hidden = true;
    candidate.value = "";
    preview.removeAttribute("src");
    if (objectUrl) URL.revokeObjectURL(objectUrl);
    objectUrl = null;
    status.textContent = "";
  }

  editor.hidden = false;
  apply.querySelector("[data-avatar-discard]").addEventListener("click", discard);
  generate.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (button.disabled) return;
    button.disabled = true;
    apply.hidden = true;
    say("Drawing your avatar…", "正在绘制头像…");
    try {
      const response = await fetch(generate.action, {
        method: "POST", body: new FormData(generate), credentials: "same-origin",
      });
      if (!response.ok || response.headers.get("Content-Type") !== "image/png") {
        const messages = {
          budget: ["The shared generation budget is exhausted.", "本期共享预算已用完。"],
          busy: ["An avatar is already being drawn.", "已有一张头像正在生成。"],
          description: ["Enter a description of up to 160 characters.", "请填写 160 字以内的画面描述。"],
          disabled: ["Image generation is disabled.", "服务端已关闭头像生成。"],
        };
        const result = await response.json().catch(() => ({}));
        say(...(messages[result.error] || ["Generation failed. Try again later.", "生成失败，请稍后再试。"]));
        return;
      }
      const file = new File([await response.blob()], "music-avatar.png", {type: "image/png"});
      const transfer = new DataTransfer();
      transfer.items.add(file);
      discard();
      candidate.files = transfer.files;
      objectUrl = URL.createObjectURL(file);
      preview.src = objectUrl;
      say("Your preview is ready.", "头像已画好，可以预览。");
    } catch {
      say("Could not load the preview. Check your connection.", "预览加载失败，请检查连接。");
    } finally {
      apply.hidden = !objectUrl;
      button.disabled = false;
    }
  });
})();
