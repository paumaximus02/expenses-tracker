(function () {
  const body = document.body;
  const syncForm = document.getElementById("sync-form");
  const syncButton = document.getElementById("sync-button");
  const syncBanner = document.getElementById("sync-banner");
  const syncBannerDismiss = document.getElementById("sync-banner-dismiss");
  const syncStartUrl = body.dataset.syncStartUrl;
  const syncStatusUrl = body.dataset.syncStatusUrl;
  const triggerBackgroundSync = body.dataset.triggerBackgroundSync === "1";
  const syncInProgressInitial = body.dataset.syncInProgress === "1";
  const notificationsToggle = document.getElementById("notifications-toggle");
  const notificationsPanel = document.getElementById("notifications-panel");
  const notificationsMenu = document.querySelector(".notifications-menu");
  let notificationsBadge = document.getElementById("notifications-badge");
  const topbar = document.querySelector(".topbar");
  const navToggle = document.getElementById("nav-toggle");
  const siteNav = document.getElementById("site-nav");

  let syncPollTimer = null;
  let wasInProgress = false;
  const SYNC_BANNER_DISMISSED_KEY = "expensesTracker.syncBannerDismissed";

  function isSyncBannerDismissed() {
    return sessionStorage.getItem(SYNC_BANNER_DISMISSED_KEY) === "1";
  }

  function setSyncBannerDismissedStorage(dismissed) {
    if (dismissed) {
      sessionStorage.setItem(SYNC_BANNER_DISMISSED_KEY, "1");
    } else {
      sessionStorage.removeItem(SYNC_BANNER_DISMISSED_KEY);
    }
  }

  let syncBannerDismissed = isSyncBannerDismissed();

  function closeSiteNav() {
    if (!topbar || !navToggle) {
      return;
    }
    topbar.classList.remove("nav-open");
    navToggle.setAttribute("aria-expanded", "false");
  }

  if (navToggle && topbar && siteNav) {
    navToggle.addEventListener("click", (event) => {
      event.stopPropagation();
      const isOpen = topbar.classList.toggle("nav-open");
      navToggle.setAttribute("aria-expanded", String(isOpen));
    });

    siteNav.querySelectorAll("a").forEach((link) => {
      link.addEventListener("click", closeSiteNav);
    });

    document.addEventListener("click", (event) => {
      if (!topbar.contains(event.target)) {
        closeSiteNav();
      }
    });
  }

  function setSyncButtonLoading(loading) {
    if (!syncButton) {
      return;
    }
    syncButton.disabled = loading;
    syncButton.classList.toggle("is-loading", loading);
    syncButton.setAttribute("aria-busy", String(loading));
  }

  function showSyncBanner() {
    if (syncBanner && !syncBannerDismissed) {
      syncBanner.hidden = false;
    }
  }

  function hideSyncBanner() {
    if (syncBanner) {
      syncBanner.hidden = true;
    }
  }

  function dismissSyncBanner() {
    syncBannerDismissed = true;
    setSyncBannerDismissedStorage(true);
    hideSyncBanner();
  }

  function clearSyncBannerDismissed() {
    syncBannerDismissed = false;
    setSyncBannerDismissedStorage(false);
  }

  function updateNotificationsBadge(count) {
    if (!notificationsToggle) {
      return;
    }

    if (count > 0) {
      if (!notificationsBadge) {
        notificationsBadge = document.createElement("span");
        notificationsBadge.className = "notifications-badge";
        notificationsBadge.id = "notifications-badge";
        notificationsToggle.appendChild(notificationsBadge);
      }
      notificationsBadge.textContent = String(count);
      return;
    }

    if (notificationsBadge) {
      notificationsBadge.remove();
      notificationsBadge = null;
    }
  }

  function escapeHtml(text) {
    const element = document.createElement("div");
    element.textContent = text;
    return element.innerHTML;
  }

  function renderNotificationsDropdown(notifications) {
    const list = document.getElementById("notifications-dropdown-list");
    const empty = document.getElementById("notifications-empty");
    if (!list) {
      return;
    }

    if (!notifications.length) {
      list.innerHTML = "";
      list.hidden = true;
      if (empty) {
        empty.hidden = false;
      }
      return;
    }

    list.hidden = false;
    if (empty) {
      empty.hidden = true;
    }

    list.innerHTML = notifications
      .map((notification) => {
        const unreadClass = notification.is_read ? "" : " notification-unread";
        const reviewLink =
          notification.review_url && notification.import_count
            ? `<a class="notification-action" href="${escapeHtml(notification.review_url)}">Review ${notification.import_count} new transaction${notification.import_count !== 1 ? "s" : ""}</a>`
            : "";
        return `<li class="notification-dropdown-item notification-${escapeHtml(notification.level)}${unreadClass}">
          <div class="notification-dropdown-title">${escapeHtml(notification.title)}</div>
          <div class="notification-dropdown-message">${escapeHtml(notification.message)}</div>
          ${reviewLink}
          <time class="local-datetime" datetime="${escapeHtml(notification.created_at)}"></time>
        </li>`;
      })
      .join("");

    formatLocalDateTimes();
  }

  function refreshNotificationsDropdown() {
    const url = notificationsMenu && notificationsMenu.dataset.notificationsUrl;
    if (!url) {
      return Promise.resolve(null);
    }

    return fetch(url, {
      headers: {
        Accept: "application/json",
        "X-Requested-With": "fetch",
      },
    })
      .then((response) => {
        if (!response.ok) {
          throw new Error("Failed to fetch notifications.");
        }
        return response.json();
      })
      .then((data) => {
        renderNotificationsDropdown(data.notifications || []);
        updateNotificationsBadge(data.unread_count || 0);
        return data;
      })
      .catch(() => null);
  }

  function showSyncToast(message, reviewUrl) {
    let wrap = document.querySelector(".flash-wrap");
    if (!wrap) {
      wrap = document.createElement("div");
      wrap.className = "flash-wrap";
      const main = document.querySelector("main.container");
      if (main) {
        main.before(wrap);
      } else {
        document.body.appendChild(wrap);
      }
    }

    const toast = document.createElement("div");
    toast.className = "flash flash-success sync-complete-toast";
    if (reviewUrl) {
      toast.innerHTML = `${message} <a href="${reviewUrl}">Review new transactions</a>.`;
    } else {
      toast.textContent = message;
    }
    wrap.appendChild(toast);
    setTimeout(() => {
      toast.remove();
    }, 8000);
  }

  function handleSyncFinished(data) {
    if (!wasInProgress) {
      return;
    }

    wasInProgress = false;
    const lastResult = data.last_result || {};
    const imported = lastResult.imported || 0;
    const error = lastResult.error;

    if (error) {
      showSyncToast(`Sync failed: ${error}`);
      return;
    }

    if (imported > 0) {
      const label = imported === 1 ? "transaction" : "transactions";
      showSyncToast(`Sync complete — ${imported} new ${label}`, data.review_url);
      return;
    }

    showSyncToast("Sync complete.");
  }

  function pollSyncStatus() {
    if (!syncStatusUrl) {
      return;
    }

    fetch(syncStatusUrl, {
      headers: {
        Accept: "application/json",
        "X-Requested-With": "fetch",
      },
    })
      .then((response) => {
        if (!response.ok) {
          throw new Error("Failed to fetch sync status.");
        }
        return response.json();
      })
      .then((data) => {
        updateNotificationsBadge(data.unread_count || 0);

        if (data.in_progress) {
          wasInProgress = true;
          showSyncBanner();
          setSyncButtonLoading(true);
          return;
        }

        if (syncPollTimer) {
          clearInterval(syncPollTimer);
          syncPollTimer = null;
        }

        setSyncButtonLoading(false);
        clearSyncBannerDismissed();
        hideSyncBanner();
        refreshNotificationsDropdown();
        handleSyncFinished(data);
      })
      .catch(() => {});
  }

  function beginSyncPolling() {
    pollSyncStatus();
    if (!syncPollTimer) {
      syncPollTimer = setInterval(pollSyncStatus, 3000);
    }
  }

  function startBackgroundSync(onlyIfStale) {
    if (!syncStartUrl) {
      return Promise.resolve(null);
    }

    const url = onlyIfStale ? `${syncStartUrl}?only_if_stale=1` : syncStartUrl;
    return fetch(url, {
      method: "POST",
      headers: {
        Accept: "application/json",
        "X-Requested-With": "fetch",
      },
    })
      .then((response) => response.json())
      .then((data) => {
        if (data.started) {
          clearSyncBannerDismissed();
          wasInProgress = true;
          showSyncBanner();
          setSyncButtonLoading(true);
          beginSyncPolling();
        } else if (data.reason === "already_running") {
          syncBannerDismissed = isSyncBannerDismissed();
          wasInProgress = true;
          showSyncBanner();
          setSyncButtonLoading(true);
          beginSyncPolling();
        } else if (data.reason === "not_connected") {
          showSyncToast("Connect Gmail in Settings before syncing.");
        }
        return data;
      })
      .catch(() => null);
  }

  if (syncForm) {
    syncForm.addEventListener("submit", (event) => {
      event.preventDefault();
      startBackgroundSync(false);
    });
  }

  if (syncBannerDismiss) {
    syncBannerDismiss.addEventListener("click", dismissSyncBanner);
  }

  if (syncStartUrl && syncStatusUrl) {
    if (syncInProgressInitial) {
      syncBannerDismissed = isSyncBannerDismissed();
      wasInProgress = true;
      showSyncBanner();
      setSyncButtonLoading(true);
      beginSyncPolling();
    } else if (triggerBackgroundSync) {
      startBackgroundSync(true);
    }
  }

  function markNotificationsRead() {
    const markReadUrl = notificationsMenu && notificationsMenu.dataset.markReadUrl;
    if (!markReadUrl) {
      return;
    }

    fetch(markReadUrl, {
      method: "POST",
      headers: {
        "X-Requested-With": "fetch",
        Accept: "application/json",
      },
    })
      .then((response) => {
        if (!response.ok) {
          throw new Error("Failed to mark notifications as read.");
        }
        return response.json();
      })
      .then(() => {
        updateNotificationsBadge(0);
        document.querySelectorAll(".notification-dropdown-item.notification-unread").forEach((item) => {
          item.classList.remove("notification-unread");
        });
      })
      .catch(() => {});
  }

  if (notificationsToggle && notificationsPanel && notificationsMenu) {
    notificationsToggle.addEventListener("click", (event) => {
      event.stopPropagation();
      if (!notificationsPanel.hidden) {
        notificationsPanel.hidden = true;
        notificationsToggle.setAttribute("aria-expanded", "false");
        return;
      }

      refreshNotificationsDropdown().then((data) => {
        notificationsPanel.hidden = false;
        notificationsToggle.setAttribute("aria-expanded", "true");
        if (data && data.unread_count > 0) {
          markNotificationsRead();
        }
      });
    });

    document.addEventListener("click", (event) => {
      if (!notificationsMenu.contains(event.target)) {
        notificationsPanel.hidden = true;
        notificationsToggle.setAttribute("aria-expanded", "false");
      }
    });
  }

  const expensesFilterForm = document.getElementById("expenses-filter-form");
  const expensesSearch = document.getElementById("expenses-search");
  const expensesMonth = document.getElementById("expenses-month");
  const expensesStatus = document.getElementById("expenses-status");

  if (expensesFilterForm) {
    let searchDebounceTimer = null;

    function updateExpensesUrl() {
      const params = new URLSearchParams(new FormData(expensesFilterForm));
      const query = params.toString();
      const url = query
        ? `${expensesFilterForm.action}?${query}`
        : expensesFilterForm.action;
      window.history.replaceState(null, "", url);
    }

    function submitExpensesFilter() {
      updateExpensesUrl();
      expensesFilterForm.requestSubmit();
    }

    function scheduleSearchSubmit(immediate) {
      if (searchDebounceTimer) {
        clearTimeout(searchDebounceTimer);
        searchDebounceTimer = null;
      }

      if (!expensesSearch) {
        submitExpensesFilter();
        return;
      }

      const query = expensesSearch.value.trim();
      if (query.length === 1) {
        return;
      }

      if (immediate || query.length === 0) {
        submitExpensesFilter();
        return;
      }

      searchDebounceTimer = setTimeout(submitExpensesFilter, 300);
    }

    if (expensesSearch) {
      expensesSearch.addEventListener("input", () => {
        scheduleSearchSubmit(false);
      });
    }

    if (expensesMonth) {
      expensesMonth.addEventListener("change", () => {
        scheduleSearchSubmit(true);
      });
    }

    if (expensesStatus) {
      expensesStatus.addEventListener("change", () => {
        scheduleSearchSubmit(true);
      });
    }
  }

  const reportFilterForm = document.getElementById("report-filter-form");
  const reportMonth = document.getElementById("report-month");
  const reportPerson = document.getElementById("report-person");

  if (reportFilterForm) {
    function submitReportFilter() {
      const params = new URLSearchParams(new FormData(reportFilterForm));
      const query = params.toString();
      const url = query
        ? `${reportFilterForm.action}?${query}`
        : reportFilterForm.action;
      window.history.replaceState(null, "", url);
      reportFilterForm.requestSubmit();
    }

    if (reportMonth) {
      reportMonth.addEventListener("change", submitReportFilter);
    }

    if (reportPerson) {
      reportPerson.addEventListener("change", submitReportFilter);
    }
  }

  const copyInviteButton = document.getElementById("copy-invite-code");
  const inviteCode = document.getElementById("invite-code");
  if (copyInviteButton && inviteCode) {
    copyInviteButton.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(inviteCode.textContent.trim());
        copyInviteButton.textContent = "Copied";
        setTimeout(() => {
          copyInviteButton.textContent = "Copy";
        }, 1500);
      } catch (_error) {
        copyInviteButton.textContent = "Copy failed";
      }
    });
  }

  function parseUtcDateTime(raw) {
    if (!raw) {
      return null;
    }
    const normalized = /[zZ]|[+-]\d{2}:\d{2}$/.test(raw) ? raw : `${raw}Z`;
    const date = new Date(normalized);
    return Number.isNaN(date.getTime()) ? null : date;
  }

  function formatLocalDateTimes() {
    document.querySelectorAll("time.local-datetime[datetime]").forEach((element) => {
      const date = parseUtcDateTime(element.getAttribute("datetime"));
      if (!date) {
        return;
      }
      element.textContent = date.toLocaleString(undefined, {
        month: "short",
        day: "numeric",
        year: "numeric",
        hour: "numeric",
        minute: "2-digit",
      });
    });
  }

  formatLocalDateTimes();
})();
