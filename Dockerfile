FROM ubuntu@sha256:d78ab76437b1afc5f01e223d6bf0172763f404bb166441328845adbef44518cb

ARG DEBIAN_FRONTEND=noninteractive
ARG WECHAT_URL=https://dldir1v6.qq.com/weixin/Universal/Linux/WeChatLinux_x86_64.deb
ARG WECHAT_SHA256=c9765e87ee5133bf4bb50d585c1814fafd995e3fb0da62c5ed07259b43dada7b
ARG WECHAT_VERSION=4.1.1.8
ARG BOWXT_VERSION=0.4.0
ARG USER_UID=1000
ARG USER_GID=1000

LABEL org.opencontainers.image.title="bowxt reproducible WeChat desktop"
LABEL org.opencontainers.image.version="${BOWXT_VERSION}"
LABEL org.opencontainers.image.wechat.version="${WECHAT_VERSION}"
LABEL org.opencontainers.image.source="https://linux.weixin.qq.com/"

ENV DEBIAN_FRONTEND=noninteractive \
    DISPLAY=:1 \
    LANG=zh_CN.UTF-8 \
    LANGUAGE=zh_CN:zh \
    LC_ALL=zh_CN.UTF-8 \
    QT_LINUX_ACCESSIBILITY_ALWAYS_ON=1 \
    NO_AT_BRIDGE=0 \
    XMODIFIERS=@im=fcitx \
    GTK_IM_MODULE=fcitx \
    QT_IM_MODULE=fcitx \
    INPUT_METHOD=fcitx \
    LIBGL_ALWAYS_SOFTWARE=1 \
    QT_XCB_GL_INTEGRATION=none \
    QTWEBENGINE_CHROMIUM_FLAGS="--disable-gpu --disable-dev-shm-usage" \
    VNC_RESOLUTION=1280x900 \
    VNC_DEPTH=24 \
    BOWXT_DB=/home/wechat/.local/share/bowxt/messages.db

COPY scripts/install-wechat.sh /usr/local/sbin/bowxt-install-wechat

RUN apt-get update && apt-get install -y --no-install-recommends \
      at-spi2-core \
      ca-certificates \
      curl \
      dbus-x11 \
      fonts-noto-cjk \
      fonts-noto-color-emoji \
      fcitx5 \
      fcitx5-chinese-addons \
      fcitx5-frontend-gtk3 \
      fcitx5-frontend-qt5 \
      imagemagick \
      iproute2 \
      libasound2t64 \
      libatk-adaptor \
      libgbm1 \
      libgl1-mesa-dri \
      libgtk-3-0t64 \
      libnss3 \
      libpulse0 \
      libxss1 \
      libxtst6 \
      locales \
      mesa-utils \
      novnc \
      procps \
      python3-pil \
      python3-pip \
      python3-pyatspi \
      python3-venv \
      supervisor \
      thunar \
      tini \
      websockify \
      x11-utils \
      x11vnc \
      xclip \
      xdg-utils \
      xfce4-panel \
      xfce4-session \
      xfce4-settings \
      xfce4-terminal \
      xfdesktop4 \
      xfwm4 \
    && sed -i 's/^# *\(zh_CN.UTF-8 UTF-8\)/\1/' /etc/locale.gen \
    && locale-gen \
    && chmod 0755 /usr/local/sbin/bowxt-install-wechat \
    && WECHAT_URL="${WECHAT_URL}" WECHAT_SHA256="${WECHAT_SHA256}" \
       WECHAT_VERSION="${WECHAT_VERSION}" /usr/local/sbin/bowxt-install-wechat \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
      libatomic1 \
      libglib2.0-bin \
      libxcb-icccm4 \
      libxcb-image0 \
      libxcb-keysyms1 \
      libxcb-render-util0 \
      xvfb \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

RUN set -eux; \
    existing_group="$(getent group "${USER_GID}" | cut -d: -f1 || true)"; \
    if [ -n "${existing_group}" ] && [ "${existing_group}" != wechat ]; then \
      groupmod --new-name wechat "${existing_group}"; \
    elif [ -z "${existing_group}" ]; then \
      groupadd --gid "${USER_GID}" wechat; \
    fi; \
    existing_user="$(getent passwd "${USER_UID}" | cut -d: -f1 || true)"; \
    if [ -n "${existing_user}" ] && [ "${existing_user}" != wechat ]; then \
      usermod --login wechat --home /home/wechat --move-home "${existing_user}"; \
      usermod --gid "${USER_GID}" --shell /bin/bash wechat; \
    elif [ -z "${existing_user}" ]; then \
      useradd --uid "${USER_UID}" --gid "${USER_GID}" --create-home --shell /bin/bash wechat; \
    fi; \
    install -d -o wechat -g wechat /home/wechat/Desktop /home/wechat/Downloads /run/bowxt

COPY . /opt/bowxt
RUN python3 -m venv --system-site-packages /opt/bowxt-venv \
    && /opt/bowxt-venv/bin/pip install --no-build-isolation --no-deps /opt/bowxt \
    && /opt/bowxt-venv/bin/python -c 'import pyatspi, PIL, bowxt; print(bowxt.__version__)'

COPY supervisord.conf /etc/supervisor/conf.d/bowxt.conf
COPY scripts/ /usr/local/bin/
COPY assets/fcitx5/ /usr/share/bowxt/fcitx5/
RUN chmod 0755 /usr/local/bin/bowxt-* /usr/local/bin/*.sh

EXPOSE 5900 6080 8787
VOLUME ["/home/wechat"]
HEALTHCHECK --interval=10s --timeout=4s --start-period=30s --retries=12 \
  CMD /usr/local/bin/bowxt-healthcheck

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/bowxt-entrypoint"]
