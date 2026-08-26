<?php

declare(strict_types=1);

// Merge this array into the existing bitrix:support.ticket.edit component
// parameters during the separately approved site rollout.
return array(
    'SET_SHOW_USER_FIELD' => array(
        'UF_MM_SERVICE_PHONE',
        'UF_MM_SERVICE_ORDER_NUMBER',
        'UF_MM_SERVICE_REQUEST_TYPE',
    ),
);
