// Spec: CONTRACTS.md（ID：<prefix>_<ULID>；ObjectRef = "<id>#v<version>"）；03 §2.1
// Mock 用确定性 ID 与伪哈希（非真实 ULID 时间戳，仅保证格式）。
const CROCKFORD = '0123456789ABCDEFGHJKMNPQRSTVWXYZ';

export function ulid(seed: number): string {
  let n = Math.abs(Math.floor(seed)) + 1;
  let s = '';
  while (n > 0) {
    s = CROCKFORD[n % 32] + s;
    n = Math.floor(n / 32);
  }
  return ('01JD' + s.padStart(22, '0')).slice(0, 26);
}

export function mkId(prefix: string, seed: number): string {
  return `${prefix}${ulid(seed)}`;
}

export function ref(id: string, version: number): string {
  return `${id}#v${version}`;
}

/** 确定性伪 sha256（64 位十六进制），仅用于演示数据。 */
export function fakeSha(input: string): string {
  let out = '';
  let h1 = 0x811c9dc5;
  for (let round = 0; round < 8; round++) {
    for (let i = 0; i < input.length; i++) {
      h1 ^= input.charCodeAt(i) + round * 131;
      h1 = Math.imul(h1, 0x01000193) >>> 0;
    }
    out += h1.toString(16).padStart(8, '0');
  }
  return `sha256:${out}`;
}
