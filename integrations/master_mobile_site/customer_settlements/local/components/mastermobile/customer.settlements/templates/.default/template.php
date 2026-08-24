<?php

if (!defined('B_PROLOG_INCLUDED') || B_PROLOG_INCLUDED !== true) {
    die();
}

$this->setFrameMode(false);
$status = (string)($arResult['status'] ?? 'temporarily_unavailable');
$state = (string)($arResult['state'] ?? '');
$isMock = ($arResult['is_mock'] ?? false) === true;
$labels = array(
    'debt' => 'Задолженность',
    'advance' => 'Ваш аванс',
    'zero' => 'Задолженности нет',
);
$formatDate = static function ($raw): string {
    try {
        return (new DateTimeImmutable((string)$raw))
            ->setTimezone(new DateTimeZone('Europe/Moscow'))
            ->format('d.m.Y H:i');
    } catch (Throwable $error) {
        return '';
    }
};
$available = in_array($status, array('available', 'stale'), true)
    && isset($labels[$state], $arResult['amount']);
$formatAmount = static function ($raw): string {
    $value = (string)$raw;
    if (!preg_match('/^(0|[1-9][0-9]*)\.([0-9]{2})$/', $value, $parts)) {
        return '0,00';
    }
    $whole = preg_replace('/\B(?=(\d{3})+(?!\d))/', ' ', $parts[1]);
    return $parts[2] === '00' ? $whole : $whole . ',' . $parts[2];
};
?>
<section class="mm-settlements" aria-labelledby="mm-settlements-title">
    <div class="profile__item mm-settlements__panel">
        <div class="profile__item-head mm-settlements__head">
            <div class="profile__item-title" id="mm-settlements-title">Состояние расчётов</div>
            <div class="mm-settlements__badges">
                <?php if ($isMock): ?>
                    <span class="mm-settlements__badge mm-settlements__badge--test">Тестовые данные</span>
                <?php endif; ?>
                <span class="mm-settlements__badge mm-settlements__badge--readonly">Только просмотр</span>
            </div>
        </div>

        <?php if ($isMock): ?>
            <div class="mm-settlements__test-notice" role="status">
                Демонстрационный пример — сумма не относится к вашему реальному балансу.
            </div>
        <?php endif; ?>

        <?php if ($available): ?>
            <div class="mm-settlements__card mm-settlements__card--<?=htmlspecialchars($state)?>">
                <div class="mm-settlements__caption"><?=htmlspecialchars($labels[$state])?></div>
                <div class="mm-settlements__amount">
                    <?=htmlspecialchars($formatAmount($arResult['amount']))?> ₽
                </div>
                <?php $asOf = $formatDate($arResult['as_of'] ?? ''); ?>
                <?php if ($asOf !== ''): ?>
                    <div class="mm-settlements__date">По данным на <?=htmlspecialchars($asOf)?></div>
                <?php endif; ?>
                <?php if ($status === 'stale'): ?>
                    <div class="mm-settlements__warning" role="status">
                        Возможны расхождения с текущим сальдо: обновление задержалось.
                    </div>
                <?php endif; ?>
            </div>
        <?php elseif (in_array($status, array('not_linked', 'ambiguous_link'), true)): ?>
            <div class="mm-settlements__message mm-settlements__message--link">
                <strong>Не удалось подтвердить связь с вашей карточкой клиента.</strong>
                <span>Обратитесь к менеджеру — финансовые данные пока не показываются.</span>
            </div>
        <?php elseif ($status === 'pilot_disabled'): ?>
            <div class="mm-settlements__message">
                <strong>Раздел пока недоступен.</strong>
                <span>Взаиморасчёты подключаются поэтапно.</span>
            </div>
        <?php else: ?>
            <div class="mm-settlements__message" role="status">
                <strong>Данные временно обновляются.</strong>
                <span>Попробуйте открыть раздел позднее.</span>
            </div>
        <?php endif; ?>

        <details class="mm-settlements__info">
            <summary>Что такое взаиморасчёты?</summary>
            <div class="mm-settlements__info-body">
                <p>Это состояние расчётов по вашей карточке клиента в 1С. Это не бонусы, не кэшбэк и не внутренний кошелёк.</p>
                <p>Данные обновляются примерно раз в час. Раздел ничего не списывает и не создаёт платежи.</p>
                <?php $syncedAt = $formatDate($arResult['synced_at'] ?? ''); ?>
                <?php if ($syncedAt !== ''): ?>
                    <p class="mm-settlements__sync">Обновлено: <?=htmlspecialchars($syncedAt)?></p>
                <?php endif; ?>
            </div>
        </details>
    </div>
</section>
