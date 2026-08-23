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
        $siteUserId = (string)$USER->GetID();
        $sessionKey = hash('sha256', 'customer-settlements-eligibility|' . $siteUserId);
        $cached = $_SESSION['MM_CUSTOMER_SETTLEMENTS_ELIGIBILITY'][$sessionKey] ?? null;
        if (
            is_array($cached)
            && (int)($cached['expires_at'] ?? 0) >= time()
            && isset($cached['visible'])
        ) {
            $mmCustomerSettlementsVisible = $cached['visible'] === true;
        } else {
            $eligibility = \MasterMobile\CustomerSettlements\Client::eligibilityForUser(
                $siteUserId
            );
            $mmCustomerSettlementsVisible = ($eligibility['status'] ?? null) === 'eligible';
            $_SESSION['MM_CUSTOMER_SETTLEMENTS_ELIGIBILITY'][$sessionKey] = array(
                'visible' => $mmCustomerSettlementsVisible,
                'expires_at' => time() + 300,
            );
        }
    }
} catch (\Throwable $error) {
    $mmCustomerSettlementsVisible = false;
}
