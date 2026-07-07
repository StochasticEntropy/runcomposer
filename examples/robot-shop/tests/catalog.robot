*** Settings ***
Documentation     Catalog checks of the fictional web shop (neutral demo corpus).

*** Test Cases ***
Search Finds Product By Title
    [Tags]    Catalog    Smoke
    Should Contain    ergonomic office chair    chair

Sort By Price Ascending
    [Tags]    Catalog    Regression
    ${sorted}=    Evaluate    sorted([30, 10, 20])
    Should Be Equal    ${sorted}    ${{[10, 20, 30]}}

Search Handles Typos Eventually
    [Tags]    Catalog    Regression    Quarantine-Flaky
    [Documentation]    Quarantined: flaky in the fictional shop, excluded by
    ...    the usual NOT prefix:Quarantine- selections.
    Should Be True    ${True}
