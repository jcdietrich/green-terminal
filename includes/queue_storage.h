#pragma once
// Persistent storage + group-aware helpers for the green-terminal message queue.
//
// The on-flash format is a JSON object stored in ESP-IDF NVS under
// "term/state_json":
//   {"q":["chunk1","chunk2",...],"g":[0,1,1,0,...],"a":[0,1,1,0,...],
//    "s":[0,0,1,0,...],"n":<next_group_id>}
//
// `q` is the chunk list, `g` is a parallel list of group ids
// (0 = standalone single-chunk message; >0 = id shared by all chunks of
// a long split message), `a` is a parallel list of alert flags (0/1),
// `s` is a parallel list of sticky flags (0/1) — sticky chunks hold the
// cycle in PAUSE_AFTER until they're dismissed — and `n` is the counter
// used to allocate the next group id.

#include <algorithm>
#include <cstdio>
#include <string>
#include <vector>

#include "esphome/core/log.h"
#include "esp_pm.h"
#include "nvs.h"
#include "nvs_flash.h"

namespace gt {

// Force-set the CPU frequency. min == max so DFS doesn't second-guess us.
// Requires CONFIG_PM_ENABLE in sdkconfig (set in the YAML).
inline void set_cpu_freq(int mhz) {
  esp_pm_config_t cfg{};
  cfg.max_freq_mhz = mhz;
  cfg.min_freq_mhz = mhz;
  cfg.light_sleep_enable = false;
  esp_err_t err = esp_pm_configure(&cfg);
  if (err != ESP_OK) {
    ESP_LOGW("term", "esp_pm_configure(%d MHz) failed: %d", mhz, (int)err);
  }
}


static constexpr const char *NVS_NS = "term";
static constexpr const char *NVS_KEY = "state_json";
static constexpr const char *LOG_TAG = "term";

// ---------- minimal hand-rolled JSON encode/decode ----------

inline std::string serialize_string(const std::string &m) {
  std::string out;
  out.reserve(m.size() + 2);
  out += '"';
  for (char c : m) {
    switch (c) {
      case '"':  out += "\\\""; break;
      case '\\': out += "\\\\"; break;
      case '\n': out += "\\n";  break;
      case '\r': out += "\\r";  break;
      case '\t': out += "\\t";  break;
      default:
        if (static_cast<unsigned char>(c) < 0x20) {
          char buf[8];
          std::snprintf(buf, sizeof(buf), "\\u%04x",
                        static_cast<unsigned char>(c));
          out += buf;
        } else {
          out += c;
        }
    }
  }
  out += '"';
  return out;
}

inline std::string serialize_state(const std::vector<std::string> &chunks,
                                   const std::vector<int> &groups,
                                   const std::vector<int> &alerts,
                                   const std::vector<int> &sticky,
                                   int next_group_id) {
  std::string out;
  out.reserve(chunks.size() * 32 + groups.size() * 4 +
              alerts.size() * 2 + sticky.size() * 2 + 32);
  out += "{\"q\":[";
  for (size_t i = 0; i < chunks.size(); i++) {
    if (i) out += ',';
    out += serialize_string(chunks[i]);
  }
  out += "],\"g\":[";
  for (size_t i = 0; i < groups.size(); i++) {
    if (i) out += ',';
    char buf[12];
    std::snprintf(buf, sizeof(buf), "%d", groups[i]);
    out += buf;
  }
  out += "],\"a\":[";
  for (size_t i = 0; i < alerts.size(); i++) {
    if (i) out += ',';
    out += alerts[i] ? '1' : '0';
  }
  out += "],\"s\":[";
  for (size_t i = 0; i < sticky.size(); i++) {
    if (i) out += ',';
    out += sticky[i] ? '1' : '0';
  }
  out += "],\"n\":";
  char buf[12];
  std::snprintf(buf, sizeof(buf), "%d", next_group_id);
  out += buf;
  out += '}';
  return out;
}

inline void skip_ws(const std::string &s, size_t &i) {
  while (i < s.size() && (s[i] == ' ' || s[i] == '\t' ||
                          s[i] == '\n' || s[i] == '\r')) {
    i++;
  }
}

inline std::string parse_string_at(const std::string &s, size_t &i) {
  std::string out;
  if (i >= s.size() || s[i] != '"') return out;
  i++;
  while (i < s.size() && s[i] != '"') {
    if (s[i] == '\\' && i + 1 < s.size()) {
      char e = s[i + 1];
      switch (e) {
        case 'n':  out += '\n'; i += 2; break;
        case 'r':  out += '\r'; i += 2; break;
        case 't':  out += '\t'; i += 2; break;
        case '"':  out += '"';  i += 2; break;
        case '\\': out += '\\'; i += 2; break;
        case '/':  out += '/';  i += 2; break;
        case 'u':
          // skip \uXXXX (no UTF-16 surrogate handling — we never emit > 0x7F)
          i += (i + 5 < s.size()) ? 6 : 2;
          break;
        default:
          out += s[i]; i++;
      }
    } else {
      out += s[i++];
    }
  }
  if (i < s.size() && s[i] == '"') i++;
  return out;
}

inline int parse_int_at(const std::string &s, size_t &i) {
  skip_ws(s, i);
  int sign = 1;
  if (i < s.size() && s[i] == '-') { sign = -1; i++; }
  int n = 0;
  while (i < s.size() && s[i] >= '0' && s[i] <= '9') {
    n = n * 10 + (s[i] - '0');
    i++;
  }
  return sign * n;
}

inline size_t find_field(const std::string &s, const std::string &key) {
  std::string needle = "\"" + key + "\"";
  size_t pos = s.find(needle);
  if (pos == std::string::npos) return std::string::npos;
  pos += needle.size();
  skip_ws(s, pos);
  if (pos < s.size() && s[pos] == ':') {
    pos++;
    skip_ws(s, pos);
  }
  return pos;
}

inline std::vector<std::string> parse_strings(const std::string &s, size_t i) {
  std::vector<std::string> out;
  skip_ws(s, i);
  if (i >= s.size() || s[i] != '[') return out;
  i++;
  while (i < s.size()) {
    skip_ws(s, i);
    if (i >= s.size()) break;
    if (s[i] == ']') break;
    if (s[i] == ',') { i++; continue; }
    if (s[i] == '"') out.push_back(parse_string_at(s, i));
    else i++;
  }
  return out;
}

inline std::vector<int> parse_ints(const std::string &s, size_t i) {
  std::vector<int> out;
  skip_ws(s, i);
  if (i >= s.size() || s[i] != '[') return out;
  i++;
  while (i < s.size()) {
    skip_ws(s, i);
    if (i >= s.size()) break;
    if (s[i] == ']') break;
    if (s[i] == ',') { i++; continue; }
    out.push_back(parse_int_at(s, i));
  }
  return out;
}

// ---------- NVS persistence ----------

inline bool save_state(const std::vector<std::string> &chunks,
                       const std::vector<int> &groups,
                       const std::vector<int> &alerts,
                       const std::vector<int> &sticky,
                       int next_group_id) {
  std::string json = serialize_state(chunks, groups, alerts, sticky,
                                     next_group_id);
  nvs_handle_t h;
  esp_err_t err = nvs_open(NVS_NS, NVS_READWRITE, &h);
  if (err != ESP_OK) {
    ESP_LOGE(LOG_TAG, "nvs_open(rw) failed: %d", static_cast<int>(err));
    return false;
  }
  err = nvs_set_blob(h, NVS_KEY, json.data(), json.size());
  if (err == ESP_OK) err = nvs_commit(h);
  nvs_close(h);
  if (err != ESP_OK) {
    ESP_LOGE(LOG_TAG, "nvs save failed: %d", static_cast<int>(err));
    return false;
  }
  ESP_LOGI(LOG_TAG, "saved state (%d chunks, %d bytes json)",
           static_cast<int>(chunks.size()), static_cast<int>(json.size()));
  return true;
}

inline bool load_state(std::vector<std::string> &chunks,
                       std::vector<int> &groups,
                       std::vector<int> &alerts,
                       std::vector<int> &sticky,
                       int &next_group_id) {
  chunks.clear();
  groups.clear();
  alerts.clear();
  sticky.clear();
  next_group_id = 1;

  nvs_handle_t h;
  esp_err_t err = nvs_open(NVS_NS, NVS_READONLY, &h);
  if (err == ESP_ERR_NVS_NOT_FOUND) {
    ESP_LOGI(LOG_TAG, "no persisted state (no namespace yet)");
    return true;
  }
  if (err != ESP_OK) {
    ESP_LOGE(LOG_TAG, "nvs_open(ro) failed: %d", static_cast<int>(err));
    return false;
  }
  size_t len = 0;
  err = nvs_get_blob(h, NVS_KEY, nullptr, &len);
  if (err == ESP_ERR_NVS_NOT_FOUND) {
    ESP_LOGI(LOG_TAG, "no persisted state (no key)");
    nvs_close(h);
    return true;
  }
  if (err != ESP_OK) {
    ESP_LOGE(LOG_TAG, "nvs_get_blob(size) failed: %d", static_cast<int>(err));
    nvs_close(h);
    return false;
  }
  std::string json;
  json.resize(len);
  err = nvs_get_blob(h, NVS_KEY, json.data(), &len);
  nvs_close(h);
  if (err != ESP_OK) {
    ESP_LOGE(LOG_TAG, "nvs_get_blob failed: %d", static_cast<int>(err));
    return false;
  }
  size_t qpos = find_field(json, "q");
  if (qpos != std::string::npos) chunks = parse_strings(json, qpos);
  size_t gpos = find_field(json, "g");
  if (gpos != std::string::npos) groups = parse_ints(json, gpos);
  size_t apos = find_field(json, "a");
  if (apos != std::string::npos) alerts = parse_ints(json, apos);
  size_t spos = find_field(json, "s");
  if (spos != std::string::npos) sticky = parse_ints(json, spos);
  size_t npos = find_field(json, "n");
  if (npos != std::string::npos) {
    size_t tmp = npos;
    next_group_id = parse_int_at(json, tmp);
  }
  // Pad parallel arrays if undersized (older save format compat).
  while (groups.size() < chunks.size()) groups.push_back(0);
  if (groups.size() > chunks.size()) groups.resize(chunks.size());
  while (alerts.size() < chunks.size()) alerts.push_back(0);
  if (alerts.size() > chunks.size()) alerts.resize(chunks.size());
  while (sticky.size() < chunks.size()) sticky.push_back(0);
  if (sticky.size() > chunks.size()) sticky.resize(chunks.size());
  // Defensive: ensure next_group_id is past any persisted ids.
  for (int g : groups) {
    if (g >= next_group_id) next_group_id = g + 1;
  }

  ESP_LOGI(LOG_TAG, "loaded %d chunks (next_group_id=%d, %d bytes)",
           static_cast<int>(chunks.size()), next_group_id,
           static_cast<int>(len));
  return true;
}

// ---------- group-aware helpers used by the state machine ----------

inline bool is_group_start(const std::vector<int> &groups, size_t i) {
  if (i >= groups.size()) return false;
  if (i == 0) return true;
  if (groups[i] == 0) return true;
  if (groups[i - 1] == 0) return true;
  return groups[i] != groups[i - 1];
}

inline bool last_in_group(const std::vector<int> &groups, size_t i) {
  if (i >= groups.size()) return false;
  if (i + 1 >= groups.size()) return true;
  if (groups[i] == 0) return true;
  if (groups[i + 1] == 0) return true;
  return groups[i + 1] != groups[i];
}

inline int group_count(const std::vector<int> &groups) {
  int total = 0;
  for (size_t i = 0; i < groups.size(); i++) {
    if (is_group_start(groups, i)) total++;
  }
  return total;
}

inline int group_position(const std::vector<int> &groups, int cur_idx) {
  if (groups.empty()) return 0;
  int pos = 0;
  int cap = std::min<int>(cur_idx + 1, static_cast<int>(groups.size()));
  for (int i = 0; i < cap; i++) {
    if (is_group_start(groups, i)) pos++;
  }
  return pos;  // 1-based
}

// ---------- shortcut expansion ----------
// Replaces `:name:` tokens in incoming messages with their UTF-8 glyph
// before the message is split/stored. Edit the table to add more.
inline std::string apply_emoji_shortcuts(const std::string &s) {
  struct E { const char *name; const char *utf8; };
  static const E table[] = {
    {":smile:",       "\xEF\x84\x98"},  // U+F118 fa-smile-o
    {":frown:",       "\xEF\x84\x99"},  // U+F119 fa-frown-o
    {":meh:",         "\xEF\x84\x9A"},  // U+F11A fa-meh-o
    {":heart:",       "\xE2\x99\xA5"},  // U+2665 ♥
    {":bolt:",        "\xE2\x9A\xA1"},  // U+26A1 ⚡
    {":lightning:",   "\xE2\x9A\xA1"},  // U+26A1 ⚡
    {":menu:",        "\xE2\x98\xB0"},  // U+2630 ☰
    {":bars:",        "\xE2\x98\xB0"},  // U+2630 ☰
    {":warning:",     "\xEF\x81\xB1"},  // U+F071 fa-warning
    {":check:",       "\xEF\x80\x8C"},  // U+F00C fa-check
    {":x:",           "\xEF\x80\x8D"},  // U+F00D fa-times
    {":times:",       "\xEF\x80\x8D"},  // U+F00D fa-times
    {":star:",        "\xEF\x80\x85"},  // U+F005 fa-star
    {":nf-heart:",    "\xEF\x80\x84"},  // U+F004 fa-heart
    {":cog:",         "\xEF\x80\x93"},  // U+F013 fa-cog
    {":home:",        "\xEF\x80\x95"},  // U+F015 fa-home
    {":bell:",        "\xEF\x83\xB3"},  // U+F0F3 fa-bell
    {":nf-bolt:",     "\xEF\x83\xA7"},  // U+F0E7 fa-bolt
    {":info:",        "\xEF\x81\x9A"},  // U+F05A fa-info-circle
    {":question:",    "\xEF\x81\x99"},  // U+F059 fa-question-circle
    {":envelope:",    "\xEF\x83\xA0"},  // U+F0E0 fa-envelope
    {":clock:",       "\xEF\x80\x97"},  // U+F017 fa-clock-o
    {":user:",        "\xEF\x80\x87"},  // U+F007 fa-user
    {":music:",       "\xEF\x80\x81"},  // U+F001 fa-music
    {":shade-light:", "\xE2\x96\x91"},  // U+2591 ░
    {":shade-med:",   "\xE2\x96\x92"},  // U+2592 ▒
    {":shade-dark:",  "\xE2\x96\x93"},  // U+2593 ▓
    {":block:",       "\xE2\x96\x88"},  // U+2588 █
    {":half-up:",     "\xE2\x96\x80"},  // U+2580 ▀
    {":half-down:",   "\xE2\x96\x84"},  // U+2584 ▄
    {":half-left:",   "\xE2\x96\x8C"},  // U+258C ▌
    {":half-right:",  "\xE2\x96\x90"},  // U+2590 ▐
    {":hbar:",        "\xE2\x94\x80"},  // U+2500 ─
    {":vbar:",        "\xE2\x94\x82"},  // U+2502 │
    {":tl:",          "\xE2\x94\x8C"},  // U+250C ┌
    {":tr:",          "\xE2\x94\x90"},  // U+2510 ┐
    {":bl:",          "\xE2\x94\x94"},  // U+2514 └
    {":br:",          "\xE2\x94\x98"},  // U+2518 ┘
    {":tee-l:",       "\xE2\x94\x9C"},  // U+251C ├
    {":tee-r:",       "\xE2\x94\xA4"},  // U+2524 ┤
    {":tee-t:",       "\xE2\x94\xAC"},  // U+252C ┬
    {":tee-b:",       "\xE2\x94\xB4"},  // U+2534 ┴
    {":cross:",       "\xE2\x94\xBC"},  // U+253C ┼
    {":endash:",      "\xE2\x80\x93"},  // U+2013 –
    {":emdash:",      "\xE2\x80\x94"},  // U+2014 —
    {":lsquo:",       "\xE2\x80\x98"},  // U+2018 ‘
    {":rsquo:",       "\xE2\x80\x99"},  // U+2019 ’
    {":ldquo:",       "\xE2\x80\x9C"},  // U+201C “
    {":rdquo:",       "\xE2\x80\x9D"},  // U+201D ”
    {":ellipsis:",    "\xE2\x80\xA6"},  // U+2026 …
    {":bullet:",      "\xE2\x80\xA2"},  // U+2022 •
    {":middot:",      "\xC2\xB7"},      // U+00B7 ·
    {":copy:",        "\xC2\xA9"},      // U+00A9 ©
    {":reg:",         "\xC2\xAE"},      // U+00AE ®
    {":tm:",          "\xE2\x84\xA2"},  // U+2122 ™
  };
  std::string out;
  out.reserve(s.size());
  size_t i = 0;
  while (i < s.size()) {
    if (s[i] == ':') {
      bool matched = false;
      for (const auto &e : table) {
        size_t n = std::strlen(e.name);
        if (i + n <= s.size() && s.compare(i, n, e.name) == 0) {
          out += e.utf8;
          i += n;
          matched = true;
          break;
        }
      }
      if (!matched) out += s[i++];
    } else {
      out += s[i++];
    }
  }
  return out;
}

// ---------- inline emphasis ----------
// Splits a line into segments where text wrapped in *asterisks* is flagged
// "blink" (and the asterisks themselves are stripped). Unmatched asterisks
// render as literal '*' characters so plain text containing isolated stars
// is preserved.
struct LineSeg {
  std::string text;
  bool blink;
};

inline std::vector<LineSeg> parse_blink_segments(const std::string &line) {
  std::vector<LineSeg> out;
  std::string buf;
  size_t i = 0;
  while (i < line.size()) {
    if (line[i] == '*') {
      size_t close = line.find('*', i + 1);
      if (close == std::string::npos) {
        buf += line[i];
        i++;
      } else {
        if (!buf.empty()) {
          out.push_back({buf, false});
          buf.clear();
        }
        out.push_back({line.substr(i + 1, close - i - 1), true});
        i = close + 1;
      }
    } else {
      buf += line[i];
      i++;
    }
  }
  if (!buf.empty()) out.push_back({buf, false});
  return out;
}

// ---------- group eviction ----------
// Remove all chunks of the front group. Returns count removed.
// Keeps all parallel vectors in sync.
inline int evict_oldest_group(std::vector<std::string> &chunks,
                              std::vector<int> &groups,
                              std::vector<int> &alerts,
                              std::vector<int> &sticky) {
  if (chunks.empty()) return 0;
  int g = groups.front();
  if (g == 0) {
    chunks.erase(chunks.begin());
    groups.erase(groups.begin());
    if (!alerts.empty()) alerts.erase(alerts.begin());
    if (!sticky.empty()) sticky.erase(sticky.begin());
    return 1;
  }
  int n = 0;
  while (!chunks.empty() && groups.front() == g) {
    chunks.erase(chunks.begin());
    groups.erase(groups.begin());
    if (!alerts.empty()) alerts.erase(alerts.begin());
    if (!sticky.empty()) sticky.erase(sticky.begin());
    n++;
  }
  return n;
}

// Find the chunk index of the Nth group (1-based). -1 if out of range.
inline int find_group_start(const std::vector<int> &groups,
                            int group_pos_1based) {
  if (group_pos_1based < 1) return -1;
  int pos = 0;
  for (size_t i = 0; i < groups.size(); i++) {
    if (is_group_start(groups, i)) {
      pos++;
      if (pos == group_pos_1based) return static_cast<int>(i);
    }
  }
  return -1;
}

// Remove a specific group by its 1-based position. Returns chunks erased.
inline int remove_group_at(std::vector<std::string> &chunks,
                           std::vector<int> &groups,
                           std::vector<int> &alerts,
                           std::vector<int> &sticky,
                           int group_pos_1based) {
  int start = find_group_start(groups, group_pos_1based);
  if (start < 0) return 0;
  size_t s = static_cast<size_t>(start);
  size_t end = s + 1;
  while (end < groups.size() && !is_group_start(groups, end)) end++;
  int n = static_cast<int>(end - s);
  chunks.erase(chunks.begin() + s, chunks.begin() + end);
  groups.erase(groups.begin() + s, groups.begin() + end);
  if (s < alerts.size()) {
    size_t aend = std::min(end, alerts.size());
    alerts.erase(alerts.begin() + s, alerts.begin() + aend);
  }
  if (s < sticky.size()) {
    size_t send = std::min(end, sticky.size());
    sticky.erase(sticky.begin() + s, sticky.begin() + send);
  }
  return n;
}

// Remove every chunk where alerts[i] == 1. Returns count removed before cur_idx
// so callers can adjust the playhead.
inline int clear_alert_chunks(std::vector<std::string> &chunks,
                              std::vector<int> &groups,
                              std::vector<int> &alerts,
                              std::vector<int> &sticky,
                              int cur_idx) {
  size_t write = 0;
  int removed_before_cursor = 0;
  for (size_t read = 0; read < chunks.size(); read++) {
    bool is_alert = read < alerts.size() && alerts[read] != 0;
    if (!is_alert) {
      if (write != read) {
        chunks[write] = chunks[read];
        groups[write] = groups[read];
        alerts[write] = alerts[read];
        if (read < sticky.size() && write < sticky.size()) {
          sticky[write] = sticky[read];
        }
      }
      write++;
    } else if (static_cast<int>(read) <= cur_idx) {
      removed_before_cursor++;
    }
  }
  chunks.resize(write);
  groups.resize(write);
  alerts.resize(write);
  if (sticky.size() > write) sticky.resize(write);
  return removed_before_cursor;
}

}  // namespace gt
