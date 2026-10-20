// Spec: 06 §7 有效期预警（90/60/30/14/7/0 天）；FR-404；01 NT-06
import { useApp, useAsync } from '../state/store';
import { useI18n, type MessageKey } from '../i18n';
import { expiringEvidence, tierSeverity } from '../domain/expiry';
import { formatDate } from '../domain/format';
import { Chip, ErrorBox, Loading } from '../components/common';
import { visiblePages } from '../domain/permissions';

export function ExpiryPage() {
  const { t, locale } = useI18n();
  const app = useApp();
  const evs = useAsync(() => app.ds.listEvidence(), []);
  if (evs.loading && !evs.data) return <Loading />;
  if (evs.error) return <ErrorBox error={evs.error} />;
  const rows = expiringEvidence(evs.data ?? [], app.ds.today());
  const canCards = visiblePages(app.role).includes('cards');
  return (
    <div>
      <div className="page-head">
        <h1>{t('expiry.title')}</h1>
      </div>
      <p className="muted">{t('expiry.intro')}</p>
      {rows.length === 0 ? (
        <div className="empty">{t('expiry.empty')}</div>
      ) : (
        <table className="table">
          <thead>
            <tr>
              <th scope="col">{t('expiry.col.evidence')}</th>
              <th scope="col">{t('expiry.col.validUntil')}</th>
              <th scope="col" className="num">
                {t('expiry.col.days')}
              </th>
              <th scope="col">{t('expiry.col.tier')}</th>
              <th scope="col">{t('expiry.col.action')}</th>
            </tr>
          </thead>
          <tbody>
            {rows.map(({ evidence: e, tier, days }) => {
              const sev = tierSeverity(tier);
              return (
                <tr key={e.id} className={`sev-${sev}`}>
                  <th scope="row">
                    <span className="mono small">{e.fact_key}</span>
                    <div className="small muted">
                      {e.source_type} · {t(`status.evidence.${e.status}` as MessageKey)}
                    </div>
                  </th>
                  <td>{formatDate(e.valid_until, locale)}</td>
                  <td className="num">{days}</td>
                  <td>
                    <Chip tone={sev === 'block' ? 'danger' : sev === 'warn' ? 'warn' : 'info'}>
                      <span aria-hidden="true">{sev === 'block' ? '⛔ ' : sev === 'warn' ? '⚠ ' : 'ℹ '}</span>
                      {t(`expiry.tier.${tier}` as MessageKey)} · {t(`expiry.sev.${sev}` as MessageKey)}
                    </Chip>
                  </td>
                  <td>
                    {canCards && (
                      <button type="button" className="btn btn-small" onClick={() => app.navigate('cards')}>
                        {t('expiry.action.renew')}
                      </button>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </div>
  );
}
