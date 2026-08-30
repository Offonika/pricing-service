<?php
/**
 * Защищённый gateway для точечной работы pricing-service с отгрузками Bitrix Sale.
 *
 * Установка на Bitrix Box:
 *   local/tools/mm_sale_shipment_gateway.php
 * Конфигурация вне web-root handler-а:
 *   local/php_interface/mm_sale_shipment_gateway.config.php
 *   <?php return [
 *       'token' => 'long-random-local-secret',
 *       'log_file' => '/var/log/mm/mm_sale_shipment_gateway.jsonl',
 *   ];
 *
 * Поддерживаются только POST JSON и четыре действия: snapshot, list, ensure,
 * update_tracking.
 * Номер трека всегда изменяется по точному shipment_id, а не у всех отгрузок заказа.
 */

declare(strict_types=1);

const STOP_STATISTICS = true;
const NO_AGENT_STATISTIC = 'Y';
const NO_KEEP_STATISTIC = 'Y';
const NOT_CHECK_PERMISSIONS = true;

use Bitrix\Main\Loader;
use Bitrix\Sale\Delivery\Services\Manager as DeliveryManager;
use Bitrix\Sale\Order;

final class MmShipmentGatewayException extends RuntimeException
{
    public function __construct(string $message, public readonly int $httpStatus = 400)
    {
        parent::__construct($message);
    }
}

function mmShipmentResponse(int $status, array $payload): never
{
    http_response_code($status);
    header('Content-Type: application/json; charset=utf-8');
    header('Cache-Control: no-store');
    echo json_encode($payload, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
    exit;
}

function mmShipmentString(mixed $value): string
{
    return trim((string)($value ?? ''));
}

function mmShipmentRequirePositiveInt(array $payload, string $field): int
{
    $value = filter_var($payload[$field] ?? null, FILTER_VALIDATE_INT);
    if ($value === false || $value < 1) {
        throw new MmShipmentGatewayException('invalid_' . $field);
    }
    return (int)$value;
}

function mmShipmentItems($shipment): array
{
    $rows = [];
    foreach ($shipment->getShipmentItemCollection() as $shipmentItem) {
        $basketItem = $shipmentItem->getBasketItem();
        $rows[] = [
            'shipment_item_id' => (int)$shipmentItem->getId(),
            'basket_item_id' => (int)$shipmentItem->getBasketId(),
            'product_id' => $basketItem ? (int)$basketItem->getProductId() : null,
            'product_xml_id' => $basketItem ? mmShipmentString($basketItem->getField('PRODUCT_XML_ID')) : '',
            'name' => $basketItem ? mmShipmentString($basketItem->getField('NAME')) : '',
            'quantity' => (string)$shipmentItem->getQuantity(),
        ];
    }
    usort($rows, static fn(array $left, array $right): int => $left['basket_item_id'] <=> $right['basket_item_id']);
    return $rows;
}

function mmShipmentRevision($shipment): string
{
    $payload = [
        'id' => (int)$shipment->getId(),
        'xml_id' => mmShipmentString($shipment->getField('XML_ID')),
        'delivery_id' => (int)$shipment->getDeliveryId(),
        'tracking_number' => mmShipmentString($shipment->getField('TRACKING_NUMBER')),
        'deducted' => mmShipmentString($shipment->getField('DEDUCTED')),
        'canceled' => mmShipmentString($shipment->getField('CANCELED')),
        'items' => mmShipmentItems($shipment),
    ];
    $encoded = json_encode($payload, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
    return hash('sha256', (string)$encoded);
}

function mmShipmentOrderRevision(Order $order): string
{
    $shipments = [];
    foreach ($order->getShipmentCollection() as $shipment) {
        if (!$shipment->isSystem()) {
            $shipments[] = mmShipmentPayload($shipment);
        }
    }
    usort($shipments, static fn(array $left, array $right): int => $left['shipment_id'] <=> $right['shipment_id']);
    return hash('sha256', (string)json_encode([
        'id' => (int)$order->getId(),
        'account_number' => mmShipmentString($order->getField('ACCOUNT_NUMBER')),
        'status_id' => mmShipmentString($order->getField('STATUS_ID')),
        'canceled' => mmShipmentString($order->getField('CANCELED')),
        'shipments' => $shipments,
    ], JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES));
}

function mmShipmentOrderMutable(Order $order): bool
{
    if (mmShipmentString($order->getField('CANCELED')) === 'Y'
        || mmShipmentString($order->getField('STATUS_ID')) === 'F') {
        return false;
    }
    return true;
}

function mmShipmentOrderPayload(Order $order): array
{
    $shipments = [];
    foreach ($order->getShipmentCollection() as $shipment) {
        if (!$shipment->isSystem()) {
            $shipments[] = mmShipmentPayload($shipment);
        }
    }
    usort($shipments, static fn(array $left, array $right): int => $left['shipment_id'] <=> $right['shipment_id']);
    return [
        'order_id' => (int)$order->getId(),
        'site_order_number' => mmShipmentString($order->getField('ACCOUNT_NUMBER')),
        'status_id' => mmShipmentString($order->getField('STATUS_ID')),
        'canceled' => mmShipmentString($order->getField('CANCELED')) === 'Y',
        'mutable' => mmShipmentOrderMutable($order),
        'revision' => mmShipmentOrderRevision($order),
        'shipments' => $shipments,
    ];
}

function mmShipmentPayload($shipment): array
{
    return [
        'shipment_id' => (int)$shipment->getId(),
        'shipment_key' => preg_replace('/^MM:/', '', mmShipmentString($shipment->getField('XML_ID'))),
        'delivery_service_id' => (int)$shipment->getDeliveryId(),
        'tracking_number' => mmShipmentString($shipment->getField('TRACKING_NUMBER')),
        'deducted' => mmShipmentString($shipment->getField('DEDUCTED')) === 'Y',
        'canceled' => mmShipmentString($shipment->getField('CANCELED')) === 'Y',
        'revision' => mmShipmentRevision($shipment),
        'items' => mmShipmentItems($shipment),
    ];
}

function mmShipmentLoadOrder(int $orderId): Order
{
    $order = Order::load($orderId);
    if ($order === null) {
        throw new MmShipmentGatewayException('order_not_found', 404);
    }
    return $order;
}

function mmShipmentLoadOrderByNumber(string $siteOrderNumber): Order
{
    if ($siteOrderNumber === '' || strlen($siteOrderNumber) > 64) {
        throw new MmShipmentGatewayException('invalid_site_order_number');
    }
    $row = \Bitrix\Sale\Internals\OrderTable::getList([
        'select' => ['ID'],
        'filter' => ['=ACCOUNT_NUMBER' => $siteOrderNumber],
        'order' => ['ID' => 'DESC'],
        'limit' => 2,
    ])->fetchAll();
    if (count($row) !== 1) {
        throw new MmShipmentGatewayException(
            count($row) === 0 ? 'order_not_found' : 'order_number_not_unique',
            count($row) === 0 ? 404 : 409
        );
    }
    return mmShipmentLoadOrder((int)$row[0]['ID']);
}

function mmShipmentValidateOrderIdentityAndState(Order $order, array $payload): void
{
    $siteOrderNumber = mmShipmentString($payload['site_order_number'] ?? '');
    if ($siteOrderNumber === ''
        || mmShipmentString($order->getField('ACCOUNT_NUMBER')) !== $siteOrderNumber) {
        throw new MmShipmentGatewayException('order_number_mismatch', 409);
    }
    if (!mmShipmentOrderMutable($order)) {
        throw new MmShipmentGatewayException('order_not_mutable', 409);
    }
}

function mmShipmentValidateOrderRevision(Order $order, array $payload): void
{
    $expectedRevision = mmShipmentString($payload['expected_order_revision'] ?? '');
    if ($expectedRevision === '' || !hash_equals(mmShipmentOrderRevision($order), $expectedRevision)) {
        throw new MmShipmentGatewayException('order_revision_conflict', 409);
    }
}

function mmShipmentWithLock(string $key, callable $callback): mixed
{
    $path = rtrim(sys_get_temp_dir(), DIRECTORY_SEPARATOR)
        . DIRECTORY_SEPARATOR . 'mm-shipment-' . hash('sha256', $key) . '.lock';
    $handle = fopen($path, 'c');
    if ($handle === false || !flock($handle, LOCK_EX)) {
        throw new MmShipmentGatewayException('shipment_lock_unavailable', 503);
    }
    try {
        return $callback();
    } finally {
        flock($handle, LOCK_UN);
        fclose($handle);
    }
}

function mmShipmentAudit(array $config, array $record): void
{
    $path = mmShipmentString($config['log_file'] ?? '');
    if ($path === '') {
        return;
    }
    $line = json_encode([
        'at' => gmdate('c'),
        'remote_addr' => mmShipmentString($_SERVER['REMOTE_ADDR'] ?? ''),
        ...$record,
    ], JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
    if ($line !== false) {
        error_log($line . PHP_EOL, 3, $path);
    }
}

function mmShipmentFind($collection, int $shipmentId)
{
    foreach ($collection as $shipment) {
        if (!$shipment->isSystem() && (int)$shipment->getId() === $shipmentId) {
            return $shipment;
        }
    }
    return null;
}

function mmShipmentRequestedItems(array $payload): array
{
    if (!isset($payload['items']) || !is_array($payload['items']) || $payload['items'] === []) {
        throw new MmShipmentGatewayException('items_required');
    }
    $result = [];
    foreach ($payload['items'] as $raw) {
        if (!is_array($raw)) {
            throw new MmShipmentGatewayException('invalid_item');
        }
        $basketId = filter_var($raw['basket_item_id'] ?? null, FILTER_VALIDATE_INT);
        $quantity = filter_var($raw['quantity'] ?? null, FILTER_VALIDATE_FLOAT);
        if ($basketId === false || $basketId < 1 || $quantity === false || $quantity <= 0) {
            throw new MmShipmentGatewayException('invalid_item');
        }
        if (isset($result[(int)$basketId])) {
            throw new MmShipmentGatewayException('duplicate_basket_item');
        }
        $result[(int)$basketId] = (float)$quantity;
    }
    ksort($result);
    return $result;
}

function mmShipmentActualQuantities($shipment): array
{
    $actual = [];
    foreach ($shipment->getShipmentItemCollection() as $item) {
        $actual[(int)$item->getBasketId()] = (float)$item->getQuantity();
    }
    ksort($actual);
    return $actual;
}

function mmShipmentQuantitiesEqual(array $left, array $right): bool
{
    if (array_keys($left) !== array_keys($right)) {
        return false;
    }
    foreach ($left as $key => $quantity) {
        if (abs($quantity - $right[$key]) > 0.0001) {
            return false;
        }
    }
    return true;
}

function mmShipmentValidateRequestedCapacity(Order $order, array $requested): void
{
    $allocated = [];
    foreach ($order->getShipmentCollection() as $shipment) {
        if ($shipment->isSystem() || mmShipmentString($shipment->getField('CANCELED')) === 'Y') {
            continue;
        }
        foreach (mmShipmentActualQuantities($shipment) as $basketId => $quantity) {
            $allocated[$basketId] = ($allocated[$basketId] ?? 0.0) + $quantity;
        }
    }
    $basket = $order->getBasket();
    foreach ($requested as $basketId => $quantity) {
        $basketItem = $basket->getItemById($basketId);
        if ($basketItem === null) {
            throw new MmShipmentGatewayException('basket_item_not_found:' . $basketId, 404);
        }
        $available = (float)$basketItem->getQuantity() - ($allocated[$basketId] ?? 0.0);
        if ($quantity - $available > 0.0001) {
            throw new MmShipmentGatewayException('shipment_quantity_exceeds_available:' . $basketId, 409);
        }
    }
}

function mmShipmentHandleList(array $payload): array
{
    $order = mmShipmentLoadOrder(mmShipmentRequirePositiveInt($payload, 'order_id'));
    $rows = [];
    foreach ($order->getShipmentCollection() as $shipment) {
        if (!$shipment->isSystem()) {
            $rows[] = mmShipmentPayload($shipment);
        }
    }
    return ['ok' => true, 'order_id' => (int)$order->getId(), 'shipments' => $rows];
}

function mmShipmentHandleSnapshot(array $payload): array
{
    $order = mmShipmentLoadOrderByNumber(mmShipmentString($payload['site_order_number'] ?? ''));
    return ['ok' => true, 'order' => mmShipmentOrderPayload($order)];
}

function mmShipmentHandleEnsure(array $payload): array
{
    $orderId = mmShipmentRequirePositiveInt($payload, 'order_id');
    $deliveryServiceId = mmShipmentRequirePositiveInt($payload, 'delivery_service_id');
    $shipmentKey = mmShipmentString($payload['shipment_key'] ?? '');
    if ($shipmentKey === '' || strlen($shipmentKey) > 120 || !preg_match('/^[A-Za-z0-9._:-]+$/', $shipmentKey)) {
        throw new MmShipmentGatewayException('invalid_shipment_key');
    }
    $requested = mmShipmentRequestedItems($payload);
    return mmShipmentWithLock('ensure:' . $orderId . ':' . $shipmentKey, static function () use (
        $orderId,
        $deliveryServiceId,
        $shipmentKey,
        $requested,
        $payload
    ): array {
        $order = mmShipmentLoadOrder($orderId);
        mmShipmentValidateOrderIdentityAndState($order, $payload);
        $collection = $order->getShipmentCollection();
        $xmlId = 'MM:' . $shipmentKey;
        foreach ($collection as $shipment) {
            if ($shipment->isSystem() || mmShipmentString($shipment->getField('XML_ID')) !== $xmlId) {
                continue;
            }
            if ((int)$shipment->getDeliveryId() !== $deliveryServiceId
                || !mmShipmentQuantitiesEqual(mmShipmentActualQuantities($shipment), $requested)) {
                throw new MmShipmentGatewayException('shipment_key_conflict', 409);
            }
            return ['ok' => true, 'created' => false, 'shipment' => mmShipmentPayload($shipment)];
        }

        mmShipmentValidateOrderRevision($order, $payload);
        mmShipmentValidateRequestedCapacity($order, $requested);
        $delivery = DeliveryManager::getObjectById($deliveryServiceId);
        if ($delivery === null) {
            throw new MmShipmentGatewayException('delivery_service_not_found', 404);
        }
        $basket = $order->getBasket();
        $shipment = $collection->createItem($delivery);
        $shipment->setField('XML_ID', $xmlId);
        foreach ($requested as $basketId => $quantity) {
            $basketItem = $basket->getItemById($basketId);
            if ($basketItem === null) {
                throw new MmShipmentGatewayException('basket_item_not_found:' . $basketId, 404);
            }
            $shipmentItem = $shipment->getShipmentItemCollection()->createItem($basketItem);
            $setResult = $shipmentItem->setQuantity($quantity);
            if (!$setResult->isSuccess()) {
                throw new MmShipmentGatewayException('shipment_quantity_rejected:' . implode('; ', $setResult->getErrorMessages()), 409);
            }
        }
        $save = $order->save();
        if (!$save->isSuccess()) {
            throw new MmShipmentGatewayException('shipment_save_failed:' . implode('; ', $save->getErrorMessages()), 409);
        }
        $readbackOrder = mmShipmentLoadOrder($orderId);
        foreach ($readbackOrder->getShipmentCollection() as $readback) {
            if (!$readback->isSystem() && mmShipmentString($readback->getField('XML_ID')) === $xmlId) {
                if (!mmShipmentQuantitiesEqual(mmShipmentActualQuantities($readback), $requested)) {
                    throw new MmShipmentGatewayException('shipment_readback_mismatch', 409);
                }
                return ['ok' => true, 'created' => true, 'shipment' => mmShipmentPayload($readback)];
            }
        }
        throw new MmShipmentGatewayException('shipment_readback_missing', 409);
    });
}

function mmShipmentHandleUpdateTracking(array $payload): array
{
    $shipmentId = mmShipmentRequirePositiveInt($payload, 'shipment_id');
    $tracking = mmShipmentString($payload['tracking_number'] ?? '');
    if ($tracking === '' || strlen($tracking) > 128) {
        throw new MmShipmentGatewayException('invalid_tracking_number');
    }
    $row = \Bitrix\Sale\Internals\ShipmentTable::getByPrimary($shipmentId, ['select' => ['ORDER_ID']])->fetch();
    if (!$row) {
        throw new MmShipmentGatewayException('shipment_not_found', 404);
    }
    $lockShipmentKey = mmShipmentString($payload['shipment_key'] ?? '') ?: (string)$shipmentId;
    return mmShipmentWithLock('tracking:' . (int)$row['ORDER_ID'] . ':' . $lockShipmentKey, static function () use (
        $shipmentId,
        $tracking,
        $row,
        $payload
    ): array {
        $order = mmShipmentLoadOrder((int)$row['ORDER_ID']);
        mmShipmentValidateOrderIdentityAndState($order, $payload);
        $shipment = mmShipmentFind($order->getShipmentCollection(), $shipmentId);
        if ($shipment === null) {
            throw new MmShipmentGatewayException('shipment_not_found', 404);
        }
        if (mmShipmentString($shipment->getField('TRACKING_NUMBER')) === $tracking) {
            return ['ok' => true, 'shipment' => mmShipmentPayload($shipment)];
        }
        mmShipmentValidateOrderRevision($order, $payload);
        $expectedRevision = $payload['expected_revision'] ?? null;
        if ($expectedRevision === null || !hash_equals(
            mmShipmentRevision($shipment),
            mmShipmentString($expectedRevision)
        )) {
            throw new MmShipmentGatewayException('shipment_revision_conflict', 409);
        }
        $shipment->setField('TRACKING_NUMBER', $tracking);
        $save = $order->save();
        if (!$save->isSuccess()) {
            throw new MmShipmentGatewayException('tracking_save_failed:' . implode('; ', $save->getErrorMessages()), 409);
        }
        $readbackOrder = mmShipmentLoadOrder((int)$row['ORDER_ID']);
        $readback = mmShipmentFind($readbackOrder->getShipmentCollection(), $shipmentId);
        if ($readback === null || mmShipmentString($readback->getField('TRACKING_NUMBER')) !== $tracking) {
            throw new MmShipmentGatewayException('tracking_readback_mismatch', 409);
        }
        return ['ok' => true, 'shipment' => mmShipmentPayload($readback)];
    });
}

$config = [];
$action = '';
try {
    if (($_SERVER['REQUEST_METHOD'] ?? '') !== 'POST') {
        throw new MmShipmentGatewayException('method_not_allowed', 405);
    }
    $documentRoot = (string)($_SERVER['DOCUMENT_ROOT'] ?? '');
    require_once $documentRoot . '/bitrix/modules/main/include/prolog_before.php';
    if (!Loader::includeModule('sale')) {
        throw new MmShipmentGatewayException('sale_module_unavailable', 503);
    }
    $configPath = $documentRoot . '/local/php_interface/mm_sale_shipment_gateway.config.php';
    $config = is_file($configPath) ? include $configPath : [];
    $configuredToken = mmShipmentString(is_array($config) ? ($config['token'] ?? '') : '');
    $authorization = mmShipmentString($_SERVER['HTTP_AUTHORIZATION'] ?? '');
    $providedToken = str_starts_with($authorization, 'Bearer ') ? substr($authorization, 7) : '';
    if ($configuredToken === '' || $providedToken === '' || !hash_equals($configuredToken, $providedToken)) {
        throw new MmShipmentGatewayException('unauthorized', 401);
    }
    $payload = json_decode((string)file_get_contents('php://input'), true, 32, JSON_THROW_ON_ERROR);
    if (!is_array($payload)) {
        throw new MmShipmentGatewayException('invalid_json');
    }
    $action = mmShipmentString($payload['action'] ?? '');
    $result = match ($action) {
        'list' => mmShipmentHandleList($payload),
        'snapshot' => mmShipmentHandleSnapshot($payload),
        'ensure' => mmShipmentHandleEnsure($payload),
        'update_tracking' => mmShipmentHandleUpdateTracking($payload),
        default => throw new MmShipmentGatewayException('unknown_action'),
    };
    mmShipmentAudit($config, [
        'action' => $action,
        'result' => 'ok',
        'order_id' => $payload['order_id'] ?? null,
        'site_order_number' => $payload['site_order_number'] ?? null,
        'shipment_id' => $payload['shipment_id'] ?? null,
        'shipment_key' => $payload['shipment_key'] ?? null,
        'idempotency_key' => $payload['idempotency_key'] ?? null,
    ]);
    mmShipmentResponse(200, $result);
} catch (MmShipmentGatewayException $exception) {
    mmShipmentAudit(is_array($config) ? $config : [], [
        'action' => $action,
        'result' => 'error',
        'error' => $exception->getMessage(),
    ]);
    mmShipmentResponse($exception->httpStatus, ['ok' => false, 'error' => $exception->getMessage()]);
} catch (JsonException) {
    mmShipmentAudit(is_array($config) ? $config : [], [
        'action' => $action,
        'result' => 'error',
        'error' => 'invalid_json',
    ]);
    mmShipmentResponse(400, ['ok' => false, 'error' => 'invalid_json']);
} catch (Throwable $exception) {
    mmShipmentAudit(is_array($config) ? $config : [], [
        'action' => $action,
        'result' => 'error',
        'error' => 'internal_error',
        'error_class' => get_class($exception),
    ]);
    mmShipmentResponse(500, ['ok' => false, 'error' => 'internal_error']);
}
