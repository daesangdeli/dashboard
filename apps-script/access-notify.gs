/**
 * 통합대시보드 접속 알림 (Google Apps Script 웹 앱)
 *
 * 대시보드에서 비밀번호를 맞게 입력하면 이 웹 앱으로 신호가 오고,
 * 이 스크립트를 배포한 구글 계정의 메일로 접속 알림을 보냅니다.
 * 사람은 구분하지 않고 시각 · 기기 · 브라우저 정보만 담습니다.
 *
 * 배포: 배포 → 새 배포 → 유형 "웹 앱" · 실행 계정 "나" · 액세스 "모든 사용자"
 *       → 나온 웹 앱 URL을 index.html 의 NOTIFY_URL 에 넣습니다.
 */
const SAME_DEVICE_MINUTES = 10;   // 같은 기기에서 이 시간 안에 다시 들어오면 메일 생략 (새로고침 중복 방지)
const DAILY_MAX = 80;             // 하루 최대 알림 수 (구글 무료 계정 발송 한도 100통 보호)

function doPost(e) { return handle_(e); }
function doGet(e) { return handle_(e); }

function handle_(e) {
  const p = (e && e.parameter) || {};
  const clip = (s, n) => String(s || '').replace(/[\r\n<>]/g, ' ').slice(0, n);
  const dev = clip(p.d, 40);

  const cache = CacheService.getScriptCache();
  if (dev && cache.get('v_' + dev)) return out_('skip');
  if (dev) cache.put('v_' + dev, '1', SAME_DEVICE_MINUTES * 60);

  const lock = LockService.getScriptLock();
  lock.tryLock(5000);
  const props = PropertiesService.getScriptProperties();
  const key = 'n_' + Utilities.formatDate(new Date(), 'Asia/Seoul', 'yyyyMMdd');
  const n = Number(props.getProperty(key) || 0);
  if (n >= DAILY_MAX) { lock.releaseLock(); return out_('cap'); }
  props.setProperty(key, String(n + 1));
  lock.releaseLock();

  const when = Utilities.formatDate(new Date(), 'Asia/Seoul', 'yyyy-MM-dd HH:mm:ss');
  const ua = clip(p.ua, 300);
  const body = [
    '통합대시보드에 접속이 있었습니다.',
    '',
    '시각   : ' + when + ' (한국시간)',
    '기기   : ' + clip(p.dev, 40) + ' · 화면 ' + clip(p.scr, 20),
    '브라우저: ' + ua,
    '주소   : ' + clip(p.url, 200),
    '오늘 ' + (n + 1) + '번째 알림',
    '',
    '※ 같은 기기에서 ' + SAME_DEVICE_MINUTES + '분 안에 다시 들어온 경우는 알리지 않습니다.',
  ].join('\n');
  MailApp.sendEmail(Session.getEffectiveUser().getEmail(), '[통합대시보드] 접속 알림 ' + when, body);
  return out_('ok');
}

function out_(s) { return ContentService.createTextOutput(s); }

/** 처음 한 번 실행해서 메일 발송 권한을 승인하고, 테스트 메일이 오는지 확인합니다. */
function testNotify() {
  handle_({ parameter: { d: 'test-' + Date.now(), dev: '테스트', scr: '-', ua: 'Apps Script 테스트 실행', url: '-' } });
}
