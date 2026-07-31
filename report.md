# 터널 연결 끊김 장애 분석 보고서

- **작성일**: 2026-07-31
- **분석 대상**: `tunnel.py`, `create_tunnel_example.sh`, `caddy_config.json`
- **증상**: 클라이언트에서 SSH로 터널을 연결한 뒤 4~5일이 지나면 연결이 끊기고, 자동 복구되지 않아 터널을 하나씩 수동으로 재연결해야 함
- **분석 근거**: 2026-07-29 03:03 ~ 07:27 서버 로그

---

## 요약

원인은 하나가 아니라 **SSH 계층의 결함 1개와 `tunnel.py`의 버그 3개가 연쇄**로 작동하는 구조다.

SSH 연결이 조용히 죽는 것이 최초 트리거이고, 그 뒤에 `tunnel.py`가 Caddy에 **지울 수 없는 좀비 route**를 남긴다. 이 좀비가 같은 호스트를 점유한 상태로 남으면, 그 호스트에 새로 붙은 터널이 **스스로 종료**해버린다. 여기에 모든 클라이언트가 남의 터널까지 지우는 orphan 청소 로직이 겹치면서 도미노처럼 번진다.

```
SSH 1개가 조용히 죽음
      ↓
health check가 "API 느림"을 "route 없음"으로 오판 → 같은 @id로 route 재생성
      ↓
Caddy 배열에 중복 @id route 발생 → 하나만 삭제 가능, 나머지는 영구 좀비
      ↓
좀비가 host를 점유 → 그 host에 재연결한 새 터널이 "다른 터널이 가져갔다"고 오판하고 자살
      ↓
남의 터널까지 지우는 orphan 청소가 2초 probe 실패로 멀쩡한 터널까지 삭제
      ↓
전체 터널이 순차적으로 붕괴 → 수동 재연결 필요
```

---

## 원인 1. SSH 세션에 keepalive와 자동 재시작이 없음

### 원인

`create_tunnel_example.sh:7`

```bash
ssh -t -R $serverPort:localhost:$localPort $domain sirtunnel $domain $serverPort
```

`ServerAliveInterval`, `TCPKeepAlive`, `ExitOnForwardFailure`가 전부 없다. 또한 SSH 프로세스가 죽었을 때 다시 띄워주는 감시자(autossh / systemd)도 없다.

### 결과

- 터널에 트래픽이 없는 동안 NAT · 방화벽 · ISP가 idle TCP 커넥션을 조용히 폐기한다. SSH는 **다음에 데이터를 쓰려고 시도할 때까지 연결이 죽은 것을 인지하지 못한다.**
- 로그 마지막 두 줄이 정확히 이 증상이다.
  ```
  Read from remote host 158.247.248.94: Operation timed out
  client_loop: send disconnect: Broken pipe
  ```
- 4~5일이라는 주기는 NAT 테이블 만료 / ISP IP 재할당 주기와 일치한다.
- 재시작 장치가 없으므로 SSH가 죽으면 그대로 끝이고, 사람이 직접 다시 붙여야 한다.
- `ExitOnForwardFailure`가 없어서, 재접속 시 서버에 좀비 sshd가 아직 `900x` 포트를 잡고 있으면 **SSH 세션은 성립하지만 포트 포워딩만 조용히 실패한다.** 겉보기에는 연결된 것처럼 보이지만 실제로는 죽은 터널이 된다.

---

## 원인 2. 재연결 시 같은 `@id`로 route를 중복 생성함 (핵심 버그)

### 원인

`tunnel.py:340` 에서 재연결 경로가 `_create_tunnel_without_delete()`를 호출한다.

`tunnel.py:342-353`

```python
def _create_tunnel_without_delete(self) -> bool:
    """Create tunnel route without deleting existing (for reconnect)."""
    url = f"{self.caddy_api}/config/apps/http/servers/sirtunnel/routes"
    config = self._get_route_config()          # {"@id": "host-port", ...}
    success, error = self._make_request("POST", url, config)
```

기존 route를 지우지 않고 **동일한 `@id`로 배열에 POST를 한 번 더** 한다. Caddy는 `@id → 경로` 매핑을 새로 들어온 것으로 덮어쓰므로, 배열에는 같은 `@id`의 route가 2개 존재하지만 **`/id/` API로 접근 가능한 것은 1개뿐**이 된다.

### 결과 (로그로 입증됨)

```
03:04:02 [INFO]    Removing orphan tunnel: api.define-space.com-9007 (port 9007 is dead)
03:04:03 [INFO]    Tunnel api.define-space.com-9007 deleted successfully
03:04:03 [INFO]    Removing orphan tunnel: api.define-space.com-9007 (port 9007 is dead)   ← 1초 뒤 같은 ID
03:04:03 [WARNING] Failed to delete tunnel api.define-space.com-9007:
                   HTTP 500: {"error":"unknown object ID 'api.define-space.com-9007'"}
```

orphan 루프는 30초 주기(`ORPHAN_CHECK_INTERVAL = 30`)인데 같은 ID가 **1초 간격으로 두 번** 처리됐다. 이는 `_get_all_routes()`가 같은 `@id`를 가진 route를 2개 반환했다는 뜻, 즉 한 번의 `_cleanup_orphan_tunnels()` 호출 안에서 for 루프가 두 번 돈 것이다. **중복 route의 직접 증거다.**

- 첫 번째는 삭제 성공 → `@id` 매핑이 제거됨
- 두 번째는 `unknown object ID` 500 → **영구히 삭제 불가능한 좀비 route**로 남음

`api.ourmemories.kr-9015`는 이미 그 상태다. 07:24와 07:27에 반복적으로 삭제를 시도하지만 계속 500이 난다. 배열에는 남아 있고, 어떤 API로도 지울 수 없다.

**추가 파급**: 좀비 route는 여전히 `api.ourmemories.kr` 호스트를 match 하면서 죽은 포트 9015를 가리킨다. Caddy는 route 배열을 순서대로 매칭하므로, 좀비가 앞에 있으면 나중에 정상 재연결한 route가 뒤에 추가되어도 **트래픽은 좀비로 가서 502가 뜬다.**

---

## 원인 3. health check가 "route 없음"과 "API 느림"을 구분하지 못함

### 원인

`tunnel.py:229-233`

```python
def _check_tunnel_health(self) -> bool:
    url = f"{self.caddy_api}/id/{self.tunnel_id}"
    success, _ = self._make_request("GET", url, timeout=5)
    return success       # 타임아웃도 False, 404도 False
```

에러 문자열을 버리고 bool만 반환한다. 5초 타임아웃과 "route가 실제로 없음"이 동일하게 취급된다.

### 결과

로그의 `Caddy API not available`은 Caddy가 죽었다는 뜻이 아니라 **5초 안에 응답하지 못했다**는 뜻이다. Caddy admin API는 config 변경마다 전역 락을 잡고 전체 config를 reload 한다. 터널이 N개면:

- N개 클라이언트 × 5초마다 health check (`HEALTH_CHECK_INTERVAL = 5`)
- N개 클라이언트 × 30초마다 전체 route 배열 조회 + 삭제 요청
- `_check_caddy_available()`는 `GET /config/`로 **전체 config를 통째로** 받아온다 (`tunnel.py:123`)

→ API 포화 → 응답 지연 → 멀쩡한 터널을 "죽었다"고 오판 → **원인 2의 중복 생성 발동** → route 배열이 커져서 더 느려짐 → 악순환.

로그의 03:03~03:05 구간이 정확히 이 패턴이다. health check 3회 연속 실패, `Caddy API not available` 반복, 그 사이에 `Tunnel recreated`가 끼어들며 중복이 생성된다.

---

## 원인 4. 좀비 route를 보고 정상 터널이 스스로 종료함 (치명타)

### 원인

`tunnel.py:334-338`

```python
# Check if another tunnel has taken over this host
if self._check_host_taken_by_other(self.host):
    self.logger.info(f"Another tunnel has taken over {self.host}, shutting down...")
    self.running = False
    return False
```

`_check_host_taken_by_other()`는 **같은 host를 match 하면서 `@id`가 다른 route가 있으면 무조건 True**를 반환한다 (`tunnel.py:194-203`). 상대 route가 살아 있는지는 확인하지 않는다.

### 결과

좀비 route는 host는 같고 id는 다르다. 예: 좀비 `api.ourmemories.kr-9015` vs 새로 붙은 `api.ourmemories.kr-9020`.

따라서 재연결한 정상 터널이 **첫 번째 health blip에서 재연결을 시도하는 순간** 좀비를 "나를 대체한 다른 터널"로 오인하고 `self.running = False`로 스스로 종료한다. `ssh -t`로 실행되므로 **SSH 세션까지 함께 죽는다.**

이것이 "재연결해줘도 얼마 못 가 또 끊긴다"의 직접적인 메커니즘이다.

---

## 원인 5. orphan 청소가 다른 클라이언트의 터널까지 삭제함

### 원인

`tunnel.py:246-275` — 모든 터널 클라이언트가 30초마다 **전체 route 목록**을 훑으면서, 자기 것이 아닌 route에 대해 2초짜리 TCP probe(`PORT_CHECK_TIMEOUT = 2`)를 하고 실패하면 삭제한다.

```python
if port and not check_port_alive(port):
    self.logger.info(f"Removing orphan tunnel: {route_id} (port {port} is dead)")
    self._delete_tunnel_by_id(route_id)
```

### 결과

- 서버 부하가 높거나 락 대기로 스케줄링이 밀려 **2초 probe가 한 번만 실패해도 멀쩡한 남의 터널이 삭제**된다. 재시도나 연속 실패 카운트가 없다.
- N개 클라이언트가 서로를 감시하고 서로를 지우려 하므로 삭제 요청이 N배로 증폭되어 원인 3의 API 포화를 가속한다.
- 한 클라이언트가 다른 클라이언트를 죽이면, 죽은 쪽은 자동 복구 수단이 없으므로(원인 1) 그대로 방치된다.

---

## 원인 6. 삭제 실패 시 정리 루프가 조기 중단됨

### 원인

`tunnel.py:186-189`

```python
if not self._delete_tunnel_by_id(route_id):
    self.logger.warning(f"Failed to delete tunnel {route_id}, skipping remaining")
    break
```

### 결과

지울 수 없는 좀비 route가 목록 앞쪽에 하나만 있어도 `break`로 빠져나가, **그 호스트의 나머지 중복 route를 전혀 정리하지 못한다.** 새 터널을 만들기 전 정리 단계(`_create_tunnel`, `tunnel.py:212`)가 무력화되므로, 수동 재연결을 해도 좀비가 그대로 남아 502가 계속된다.

---

## 원인 7. 락을 잡은 채로 최대 60초 sleep

### 원인

`tunnel.py:301` 에서 health 스레드가 `self.lock`을 획득한 상태로 `_reconnect()`를 호출하고, `_reconnect()` 내부에서 `time.sleep(self.reconnect_delay)`를 한다 (`tunnel.py:328`). `reconnect_delay`는 최대 60초까지 지수 증가한다 (`MAX_RECONNECT_DELAY = 60`).

### 결과

orphan 스레드(`tunnel.py:285`)가 같은 락을 기다리며 최대 60초 블록된다. 그래서 30초 주기여야 할 orphan 청소가 로그에서는 07:24 → 07:27로 3분 간격으로 나타난다. 장애 상황에서 정리 동작이 오히려 더 느려지는 구조다.

---

## 종합 인과 관계

| # | 원인 | 직접적 결과 |
|---|------|-------------|
| 1 | SSH keepalive · 자동 재시작 없음 | 4~5일 주기로 조용히 끊김, 자동 복구 불가 → 수동 재연결 |
| 2 | 재연결 시 같은 `@id`로 중복 POST | 삭제 불가능한 좀비 route 발생, 해당 호스트 502 |
| 3 | health check가 타임아웃과 404를 동일 취급 | API 지연을 장애로 오판 → 원인 2 발동, API 포화 악순환 |
| 4 | 좀비를 "다른 터널"로 오인 | 정상 터널이 스스로 종료 → SSH까지 동반 종료 |
| 5 | 모든 클라이언트가 남의 터널을 probe 후 삭제 | 일시적 probe 실패로 멀쩡한 터널 삭제, 도미노 확산 |
| 6 | 삭제 실패 시 `break` | 좀비가 남아 정리 단계가 무력화, 수동 재연결도 실패 |
| 7 | 락 보유 상태로 최대 60초 sleep | 장애 시 정리 동작이 지연되어 복구가 더 느려짐 |

---

## 해결 방안

### A. 즉시 조치 — 좀비 route 제거 (서버)

`/id/` DELETE로는 제거되지 않으므로 배열을 통째로 교체해야 한다.

```bash
curl -s localhost:2019/config/apps/http/servers/sirtunnel/routes \
  | jq 'unique_by(."@id")' > /tmp/routes.json

curl -X PATCH -H 'Content-Type: application/json' \
  -d @/tmp/routes.json \
  localhost:2019/config/apps/http/servers/sirtunnel/routes
```

적용 후 `tunnel_cleanup.py list`로 중복이 사라졌는지 확인한다.

### B. SSH 계층 — 연결 유지 및 자동 복구

클라이언트 `~/.ssh/config`:

```
Host *
    ServerAliveInterval 20
    ServerAliveCountMax 3
    TCPKeepAlive yes
    ExitOnForwardFailure yes
```

- `ServerAliveInterval 20` + `ServerAliveCountMax 3` → 죽은 커넥션을 60초 안에 감지
- `ExitOnForwardFailure yes` → 포트 포워딩 실패 시 SSH가 즉시 종료되어, "연결된 척하는 죽은 터널"을 방지하고 감시자가 재시도하게 만듦

그리고 `autossh -M 0 -t ...` 또는 systemd 서비스(`Restart=always`, `RestartSec=10`)로 감싼다. **이것이 수동 재연결을 없애는 핵심이다.**

서버 `/etc/ssh/sshd_config`:

```
ClientAliveInterval 30
ClientAliveCountMax 3
```

→ 좀비 sshd가 `900x` 포트를 붙잡고 있는 시간을 줄여, 재접속 시 포워딩 실패를 방지한다.

### C. `tunnel.py` 수정

1. **`_create_tunnel_without_delete` → 멱등 갱신으로 교체.** `PATCH /id/{tunnel_id}`로 기존 객체를 치환하고, 404일 때만 배열에 POST 한다. → 원인 2 원천 차단
2. **health check가 에러 종류를 구분하도록 변경.** HTTP 응답(route 없음)과 타임아웃/네트워크 실패(API 지연)를 분리해, 후자일 때는 재생성하지 않고 대기한다. → 원인 3 해소
3. **`_check_host_taken_by_other` 기반 자살 로직 제거.** 유지한다면 상대 route의 포트가 실제로 살아 있을 때만 종료하도록 조건을 추가한다. → 원인 4 해소
4. **orphan 청소를 클라이언트에서 분리.** 서버의 cron / systemd timer에서 `tunnel_cleanup.py` 하나만 수행하게 하고, 연속 3회 이상 probe 실패한 경우에만 삭제하도록 한다. 삭제 실패 시 `break` 대신 `continue`로 바꾼다. → 원인 5, 6 해소
5. **락 보유 중 sleep 제거.** 백오프 대기는 락을 놓은 상태에서 수행한다. → 원인 7 해소
6. **API 부하 완화.** `_check_caddy_available()`가 `GET /config/` 전체를 받지 않도록 경량 엔드포인트로 변경하고, health/orphan 주기에 jitter를 준다.
