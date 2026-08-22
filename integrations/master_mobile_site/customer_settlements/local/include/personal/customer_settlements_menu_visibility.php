<?php

declare(strict_types=1);

$mmCustomerSettlementsVisible = false;
$mmCustomerSettlementsLink = '/personal/settlements/';

try {
    global $USER;
    $clientPath = $_SERVER['DOCUMENT_ROOT']
        . '/local/components/mastermobile/customer.settlements/lib/client.php';
    if (is_object($USER) && $USER->IsAuthorized() && is_file($clientPath)) {
        require_once $clientPath;
        $summary = \MasterMobile\CustomerSettlements\Client::summaryForUser(
            (string)$USER->GetID(),
            true
        );
        $mmCustomerSettlementsVisible = ($summary['status'] ?? null) !== 'pilot_disabled'
            && ($summary['_transport_error'] ?? false) !== true;
    }
} catch (\Throwable $error) {
    $mmCustomerSettlementsVisible = false;
}
