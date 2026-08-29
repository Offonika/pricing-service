<?php
/**
 * Мост «трек СДЭК из Bitrix24 CRM -> заказ интернет-магазина -> 1С».
 *
 * Задача 3074 / контур задачи 3073. Приложение «Отправки: СДЭК» кладёт номер накладной
 * в поле сделки UF_CRM_OT_TRACKING, но не возвращает его в заказ на сайте. Штатная
 * выгрузка CommerceML формирует свойство «Идентификатор отправления» из поля уровня
 * заказа b_sale_order.TRACKING_NUMBER, поэтому в 1С трек не попадает никогда.
 *
 * Скрипт читает сделки через REST Bitrix24 и записывает трек в заказ сайта штатным
 * API. Запись через CSaleOrder::Update обновляет DATE_UPDATE, и заказ попадает в
 * ближайшую выгрузку по фильтру >=DATE_UPDATE (CSaleExport::prepareFilter).
 *
 * Запуск (CLI, из docroot сайта):
 *   php mm_site_cdek_track_sync.php --dry-run
 *   php mm_site_cdek_track_sync.php --days=3 --limit=3
 *   php mm_site_cdek_track_sync.php --order=240171
 *
 * Конфигурация: local/php_interface/mm_site_cdek_track_sync.config.php
 *   <?php return ['webhook' => 'https://crm.master-mobile.ru/rest/<id>/<token>/'];
 */

declare(strict_types=1);

const STOP_STATISTICS = true;
const NO_AGENT_STATISTIC = 'Y';
const NO_KEEP_STATISTIC = 'Y';
const NOT_CHECK_PERMISSIONS = true;

if (PHP_SAPI !== 'cli') {
    fwrite(STDERR, "CLI only\n");
    exit(1);
}

// Скрипт может лежать в подкаталоге сайта, поэтому корень ищем вверх по дереву
// до каталога, где реально есть ядро Bitrix.
if (empty($_SERVER['DOCUMENT_ROOT']) || !is_file($_SERVER['DOCUMENT_ROOT'] . '/bitrix/modules/main/include/prolog_before.php')) {
    $candidate = __DIR__;
    while ($candidate !== '' && $candidate !== '/' && !is_file($candidate . '/bitrix/modules/main/include/prolog_before.php')) {
        $candidate = dirname($candidate);
    }
    if (!is_file($candidate . '/bitrix/modules/main/include/prolog_before.php')) {
        fwrite(STDERR, "Bitrix document root not found\n");
        exit(1);
    }
    $_SERVER['DOCUMENT_ROOT'] = $candidate;
}

require_once $_SERVER['DOCUMENT_ROOT'] . '/bitrix/modules/main/include/prolog_before.php';

final class SiteCdekTrackSync
{
    /** Поле сделки с номером накладной, заполняется приложением «Отправки: СДЭК». */
    private const DEAL_FIELD_TRACK = 'UF_CRM_OT_TRACKING';

    /** Поле сделки с номером заказа интернет-магазина. */
    private const DEAL_FIELD_ORDER = 'UF_CRM_1772784329053';

    /** 1С обновляет свойства только у документов не старше 31 дня. */
    private const MAX_DAYS = 31;

    /**
     * Код служебного свойства заказа. Штатная выгрузка CommerceML отдаёт свойства
     * заказа по их названию, поэтому имя свойства должно быть ровно
     * «Идентификатор отправления» — именно его ждёт обработчик в 1С.
     */
    private const ORDER_PROP_CODE = 'MM_TRACKING_NUMBER';

    private string $webhook;
    private bool $dryRun;
    private int $days;
    private int $limit;
    private string $singleOrder;
    private bool $force;
    private string $logFile;

    /** @var array<string,int> */
    private array $stats = [
        'deals' => 0,
        'written' => 0,
        'already_same' => 0,
        'order_not_found' => 0,
        'multiple_shipments' => 0,
        'no_order_number' => 0,
        'write_failed' => 0,
        'verify_failed' => 0,
    ];

    public function __construct(array $argv)
    {
        $this->dryRun = in_array('--dry-run', $argv, true);
        $this->days = (int)($this->arg($argv, '--days') ?: 3);
        $this->limit = (int)($this->arg($argv, '--limit') ?: 0);
        $this->singleOrder = trim((string)$this->arg($argv, '--order'));
        // --force: вернуть заказ в очередь выгрузки, даже если трек на сайте уже совпадает.
        // Нужно для заказов, записанных до появления логики повторной постановки в очередь.
        $this->force = in_array('--force', $argv, true);

        if ($this->days < 1 || $this->days > self::MAX_DAYS) {
            $this->days = min(max($this->days, 1), self::MAX_DAYS);
        }

        $configPath = $_SERVER['DOCUMENT_ROOT'] . '/local/php_interface/mm_site_cdek_track_sync.config.php';
        if (!is_file($configPath)) {
            $this->fail('config not found: ' . $configPath);
        }
        $config = include $configPath;
        $this->webhook = rtrim((string)($config['webhook'] ?? ''), '/');
        if ($this->webhook === '') {
            $this->fail('config: empty webhook');
        }

        $logDir = $_SERVER['DOCUMENT_ROOT'] . '/local/php_interface/logs';
        if (!is_dir($logDir)) {
            @mkdir($logDir, 0775, true);
        }
        // Каталог логов принадлежит пользователю сайта. Ручные прогоны под другой
        // учётной записью пишут журнал рядом со скриптом, вывод в stdout не теряется.
        if (!is_writable($logDir)) {
            $logDir = __DIR__;
        }
        $this->logFile = $logDir . '/mm_site_cdek_track_sync.log';
    }

    public function run(): int
    {
        if (!\Bitrix\Main\Loader::includeModule('sale')) {
            $this->fail('sale module is not available');
        }

        $mode = $this->dryRun ? 'DRY-RUN' : 'WRITE';
        $this->log(sprintf('start mode=%s days=%d limit=%d order=%s',
            $mode, $this->days, $this->limit, $this->singleOrder !== '' ? $this->singleOrder : '-'));

        foreach ($this->fetchDeals() as $deal) {
            $this->stats['deals']++;
            $this->processDeal($deal);
            if ($this->limit > 0 && $this->stats['written'] >= $this->limit) {
                $this->log('limit reached, stop');
                break;
            }
        }

        $this->log('done ' . json_encode($this->stats, JSON_UNESCAPED_UNICODE));
        return $this->stats['write_failed'] > 0 || $this->stats['verify_failed'] > 0 ? 2 : 0;
    }

    private function processDeal(array $deal): void
    {
        $dealId = (string)($deal['ID'] ?? '');
        $track = trim((string)($deal[self::DEAL_FIELD_TRACK] ?? ''));
        $orderNumber = trim((string)($deal[self::DEAL_FIELD_ORDER] ?? ''));

        if ($track === '') {
            return;
        }
        if ($orderNumber === '') {
            $this->stats['no_order_number']++;
            $this->log(sprintf('deal=%s skip: no order number', $dealId));
            return;
        }

        $order = $this->findOrder($orderNumber);
        if ($order === null) {
            $this->stats['order_not_found']++;
            $this->log(sprintf('deal=%s order=%s skip: order not found on site', $dealId, $orderNumber));
            return;
        }

        // Единственное поле сделки не сообщает, к какой части разделённого заказа
        // относится трек. Такой заказ обрабатывает shipment gateway по точному ID.
        // Fail closed: не пишем один номер во все физические отгрузки и в общее
        // поле заказа, откуда он уйдёт в 1С как будто относится ко всему заказу.
        if ($this->hasMultiplePhysicalShipments((int)$order['ID'])) {
            $this->stats['multiple_shipments']++;
            $this->log(sprintf(
                'deal=%s order=%s skip: multiple physical shipments require exact shipment_id',
                $dealId,
                $orderNumber
            ));
            return;
        }

        $current = trim((string)($order['TRACKING_NUMBER'] ?? ''));
        $exported = strtoupper(trim((string)($order['UPDATED_1C'] ?? ''))) === 'Y';

        if ($current === $track && !($this->force && $exported)) {
            $this->stats['already_same']++;
            return;
        }

        if ($current === $track) {
            $this->log(sprintf('deal=%s order=%s(id=%s) трек уже на сайте, ставлю заказ в очередь выгрузки',
                $dealId, $orderNumber, $order['ID']));
        }

        if ($current !== $track) {
            $this->log(sprintf('deal=%s order=%s(id=%s) track: %s -> %s',
                $dealId, $orderNumber, $order['ID'], $current === '' ? 'пусто' : $current, $track));
        }

        if ($this->dryRun) {
            return;
        }

        $fields = ['TRACKING_NUMBER' => $track];

        // Заказ, помеченный как уже выгруженный (UPDATED_1C = Y), сайт повторно в 1С
        // не отдаёт: обновления одного лишь TRACKING_NUMBER для попадания в выгрузку
        // недостаточно. Возвращаем заказ в очередь обмена, иначе трек не доедет.
        if ($exported) {
            $fields['UPDATED_1C'] = 'N';
        }

        // Выгрузка CommerceML отдаёт «Идентификатор отправления» в блоке документа
        // отгрузки: значение берётся из отгрузки, а не из заказа. Поэтому пишем в
        // отгрузку, а поле заказа заполняем дополнительно — им пользуются другие места.
        $orderSideOk = $this->writeOrderTracking((int)$order['ID'], $track);
        if (!$orderSideOk) {
            $this->stats['write_failed']++;
            $this->log(sprintf('deal=%s order=%s ORDER WRITE FAILED', $dealId, $orderNumber));
            return;
        }

        $updated = \CSaleOrder::Update((int)$order['ID'], $fields);
        if (!$updated) {
            $this->stats['write_failed']++;
            $ex = $GLOBALS['APPLICATION']->GetException();
            $this->log(sprintf('deal=%s order=%s WRITE FAILED: %s',
                $dealId, $orderNumber, $ex ? $ex->GetString() : 'unknown'));
            return;
        }

        $check = $this->findOrder($orderNumber);
        if ($check === null || trim((string)($check['TRACKING_NUMBER'] ?? '')) !== $track) {
            $this->stats['verify_failed']++;
            $this->log(sprintf('deal=%s order=%s VERIFY FAILED: значение не сохранилось', $dealId, $orderNumber));
            return;
        }

        $this->stats['written']++;
    }

    /**
     * Записывает номер накладной в служебное свойство заказа и в отгрузки.
     *
     * В выгрузку CommerceML попадает именно значение свойства заказа: блок с тегом
     * SALE_EXPORT_TRACKING_NUMBER относится к ветке отгрузок, которая на этом сайте
     * не используется. У ранее созданных заказов значения нового свойства нет, поэтому
     * работаем через CSaleOrderPropsValue: обновляем строку или создаём её.
     * Отгрузку заполняем дополнительно, чтобы номер был виден в админке.
     */
    private function writeOrderTracking(int $orderId, string $track): bool
    {
        $orderRow = \CSaleOrder::GetByID($orderId);
        if (!$orderRow) {
            return false;
        }

        $prop = \CSaleOrderProps::GetList(
            ['SORT' => 'ASC'],
            ['CODE' => self::ORDER_PROP_CODE, 'PERSON_TYPE_ID' => $orderRow['PERSON_TYPE_ID']],
            false,
            false,
            ['ID', 'NAME', 'CODE']
        )->Fetch();

        if (!$prop) {
            $this->log(sprintf('order=%d: свойство %s не заведено для типа плательщика %s',
                $orderId, self::ORDER_PROP_CODE, (string)$orderRow['PERSON_TYPE_ID']));
            return false;
        }

        $value = \CSaleOrderPropsValue::GetList(
            ['ID' => 'DESC'],
            ['ORDER_ID' => $orderId, 'ORDER_PROPS_ID' => $prop['ID']],
            false,
            false,
            ['ID', 'VALUE']
        )->Fetch();

        if ($value) {
            if (trim((string)$value['VALUE']) !== $track
                && !\CSaleOrderPropsValue::Update((int)$value['ID'], ['VALUE' => $track])) {
                $this->log(sprintf('order=%d: не удалось обновить значение свойства', $orderId));
                return false;
            }
        } else {
            $added = \CSaleOrderPropsValue::Add([
                'ORDER_ID' => $orderId,
                'ORDER_PROPS_ID' => $prop['ID'],
                'NAME' => $prop['NAME'],
                'CODE' => $prop['CODE'],
                'VALUE' => $track,
            ]);
            if (!$added) {
                $this->log(sprintf('order=%d: не удалось создать значение свойства', $orderId));
                return false;
            }
        }

        $this->writeShipmentTracking($orderId, $track);

        return true;
    }

    /**
     * Дублирует номер только в единственную несистемную отгрузку.
     * Для нескольких частей запись выполняет shipment gateway по точному shipment_id.
     */
    private function writeShipmentTracking(int $orderId, string $track): void
    {
        $order = \Bitrix\Sale\Order::load($orderId);
        if ($order === null) {
            return;
        }

        $physical = [];
        foreach ($order->getShipmentCollection() as $shipment) {
            if (!$shipment->isSystem()) {
                $physical[] = $shipment;
            }
        }
        if (count($physical) !== 1
            || trim((string)$physical[0]->getField('TRACKING_NUMBER')) === $track) {
            return;
        }
        $physical[0]->setField('TRACKING_NUMBER', $track);
        $result = $order->save();
        if (!$result->isSuccess()) {
            $this->log('shipment save warnings: ' . implode('; ', $result->getErrorMessages()));
        }
    }

    private function hasMultiplePhysicalShipments(int $orderId): bool
    {
        $order = \Bitrix\Sale\Order::load($orderId);
        if ($order === null) {
            return false;
        }
        $count = 0;
        foreach ($order->getShipmentCollection() as $shipment) {
            if (!$shipment->isSystem() && ++$count > 1) {
                return true;
            }
        }
        return false;
    }

    /** @return array<string,mixed>|null */
    private function findOrder(string $accountNumber): ?array
    {
        $rs = \CSaleOrder::GetList(
            ['ID' => 'DESC'],
            ['ACCOUNT_NUMBER' => $accountNumber],
            false,
            ['nTopCount' => 1],
            ['ID', 'ACCOUNT_NUMBER', 'TRACKING_NUMBER', 'DATE_INSERT', 'DATE_UPDATE', 'UPDATED_1C']
        );
        $row = $rs->Fetch();

        return $row ?: null;
    }

    /** @return iterable<array<string,mixed>> */
    private function fetchDeals(): iterable
    {
        $filter = ['!' . self::DEAL_FIELD_TRACK => ''];
        if ($this->singleOrder !== '') {
            $filter[self::DEAL_FIELD_ORDER] = $this->singleOrder;
        } else {
            $filter['>DATE_CREATE'] = date('Y-m-d\TH:i:sP', strtotime('-' . $this->days . ' days'));
        }

        $start = 0;
        do {
            $response = $this->rest('crm.deal.list', [
                'filter' => $filter,
                'select' => ['ID', self::DEAL_FIELD_ORDER, self::DEAL_FIELD_TRACK, 'DATE_CREATE'],
                'order' => ['ID' => 'ASC'],
                'start' => $start,
            ]);

            $batch = $response['result'] ?? [];
            foreach ($batch as $deal) {
                yield $deal;
            }

            $start = isset($response['next']) ? (int)$response['next'] : 0;
        } while ($start > 0 && $batch);
    }

    private function rest(string $method, array $params): array
    {
        $ch = curl_init($this->webhook . '/' . $method . '.json');
        curl_setopt_array($ch, [
            CURLOPT_POST => true,
            CURLOPT_POSTFIELDS => http_build_query($params),
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_TIMEOUT => 60,
        ]);
        $body = curl_exec($ch);
        $error = curl_error($ch);
        curl_close($ch);

        if ($body === false) {
            $this->fail('REST ' . $method . ' failed: ' . $error);
        }

        $decoded = json_decode((string)$body, true);
        if (!is_array($decoded) || isset($decoded['error'])) {
            $this->fail('REST ' . $method . ' error: ' . substr((string)$body, 0, 300));
        }

        return $decoded;
    }

    private function log(string $message): void
    {
        $line = '[' . date('Y-m-d H:i:s') . '] ' . $message;
        echo $line . PHP_EOL;
        @file_put_contents($this->logFile, $line . PHP_EOL, FILE_APPEND);
    }

    private function fail(string $message): void
    {
        $this->log('FATAL: ' . $message);
        exit(1);
    }

    private function arg(array $argv, string $name): ?string
    {
        foreach ($argv as $item) {
            if (strpos($item, $name . '=') === 0) {
                return substr($item, strlen($name) + 1);
            }
        }

        return null;
    }
}

exit((new SiteCdekTrackSync($argv))->run());
