*** Settings ***
Documentation     Payments checks of the fictional web shop (neutral demo corpus).

*** Test Cases ***
Visa Payment Succeeds
    [Tags]    Payments    Smoke
    Log    charging a visa card
    Should Be Equal    ${True}    ${True}

Mastercard Payment Succeeds
    [Tags]    Payments    Regression
    Log    charging a mastercard
    Should Be True    ${1} == ${1}

Declined Card Shows Error
    [Tags]    Payments    Regression
    Log    declining a card on purpose
    Should Contain    payment declined    declined

Refund Restores Balance
    [Tags]    Payments    Regression
    ${balance}=    Evaluate    100 - 25 + 25
    Should Be Equal As Integers    ${balance}    100

Expired Card Is Rejected Loudly
    [Tags]    Payments    Regression
    [Documentation]    Deliberately red: the demo corpus ships one failing test.
    Fail    simulated defect: expired card was accepted
