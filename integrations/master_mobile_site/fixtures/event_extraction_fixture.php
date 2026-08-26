<?php

declare(strict_types=1);

require_once dirname(__DIR__) . '/service_ticket_bridge.php';

use MasterMobile\SiteServiceRequests\ServiceTicketBridge;

$actual = ServiceTicketBridge::extractEventReference(
    'OnAfterTicketUpdate',
    array(array('ID' => 741, 'MID' => 1201))
);
$expected = array(
    'ticketId' => 741,
    'messageId' => 1201,
    'eventType' => 'message.created',
);
if ($actual !== $expected) {
    fwrite(STDERR, "event extraction fixture mismatch\n");
    exit(1);
}

echo json_encode($actual, JSON_UNESCAPED_SLASHES) . PHP_EOL;
