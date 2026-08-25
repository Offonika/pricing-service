<?php

declare(strict_types=1);

require_once dirname(__DIR__) . '/service_ticket_bridge.php';

use MasterMobile\SiteServiceRequests\BridgeFailure;
use MasterMobile\SiteServiceRequests\ServiceTicketBridge;

$method = new ReflectionMethod(ServiceTicketBridge::class, 'ticketDateIsClosed');
$method->setAccessible(true);

$actual = array(
    'null' => $method->invoke(null, null),
    'empty' => $method->invoke(null, ''),
    'zeroDate' => $method->invoke(null, '0000-00-00 00:00:00'),
    'closed' => $method->invoke(null, '2026-08-25 12:34:56'),
    'malformedError' => null,
);

try {
    $method->invoke(null, 'closed');
} catch (BridgeFailure $error) {
    $actual['malformedError'] = $error->errorCode();
}

$expected = array(
    'null' => false,
    'empty' => false,
    'zeroDate' => false,
    'closed' => true,
    'malformedError' => 'ticket_date_close_invalid',
);

if ($actual !== $expected) {
    fwrite(STDERR, "ticket date close fixture mismatch\n");
    exit(1);
}

echo json_encode($actual, JSON_UNESCAPED_SLASHES) . PHP_EOL;
