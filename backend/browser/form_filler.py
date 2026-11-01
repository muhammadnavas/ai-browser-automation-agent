"""
browser/form_filler.py
----------------------
Reusable form filling module built on top of browser.actions.

Provides high-level workflows for:
- Login forms
- Registration forms
- Survey forms
- Job application forms
- File uploads
- Auto-filling saved user information
- **Zero-config autofill** via FormDiscovery (no manual mapping needed)

This module reuses the low-level browser actions and exposes
generic functions that can be integrated with the AI agent.
"""

import logging
from typing import Any, Dict, Optional

from playwright.async_api import Page

from browser import actions
from browser.form_discovery import FormDiscovery
from models.schemas import FormDiscoveryResult, FieldType

logger = logging.getLogger("browser.form_filler")


class FormFiller:
    """
    High-level reusable form filling module.

    This class builds on top of browser.actions and provides
    reusable workflows for filling different kinds of forms.
    """

    def __init__(self, page: Page):
        self.page = page
        
            # ---------------------------------------------------------------------------
    # Generic Form Filling
    # ---------------------------------------------------------------------------

    async def fill_form(
        self,
        field_mapping: Dict[str, Dict[str, str]],
        form_data: Dict[str, Any],
        submit_selector: Optional[str] = None,
    ) -> bool:
        """
        Generic form filling engine.

        Fills different types of form fields using the supplied
        field mapping and form data.

        Supports:
            - Text inputs
            - Dropdowns
            - Checkboxes
            - Radio buttons
            - File uploads

        Args:
            field_mapping:
                Dictionary describing each form field, including
                its selector and field type.

            form_data:
                Dictionary containing values to fill.

            submit_selector:
                Optional selector of the submit button.

        Returns:
            True if the form was filled successfully,
            False otherwise.
        """

        try:

            for field_name, config in field_mapping.items():

                if field_name not in form_data:
                    continue

                value = form_data[field_name]
                field_type = config.get("type", "text")
                selector = config.get("selector")

                if field_type == "text":

                    success = await actions.fill_field(
                        self.page,
                        selector,
                        str(value),
                    )

                elif field_type == "dropdown":

                    success = await actions.select_option(
                        self.page,
                        selector,
                        label=str(value),
                    )

                elif field_type == "checkbox":

                    success = await actions.check_checkbox(
                        self.page,
                        selector,
                        checked=bool(value),
                    )

                elif field_type == "radio":

                    success = await actions.select_radio(
                        self.page,
                        config["name"],
                        str(value),
                    )

                elif field_type == "file":

                    locator = self.page.locator(selector).first
                    await locator.set_input_files(str(value))
                    success = True

                else:

                    logger.warning(f"Unsupported field type: {field_type}")
                    success = False

                if not success:

                    logger.error(f"Failed filling field: {field_name}")
                    return False

            if submit_selector:

                success = await actions.click(
                    self.page,
                    submit_selector,
                )

                if not success:
                    return False

            return True

        except Exception as exc:

            logger.exception(f"Form filling failed: {exc}")
            return False
        
            # ---------------------------------------------------------------------------
    # Login Forms
    # ---------------------------------------------------------------------------

    async def login(
        self,
        username: str,
        password: str,
        username_selector: str,
        password_selector: str,
        submit_selector: str,
    ) -> bool:
        """
        Fill and submit a login form.

        Args:
            username:
                Username or email.

            password:
                User password.

            username_selector:
                Selector for username/email field.

            password_selector:
                Selector for password field.

            submit_selector:
                Selector for login button.

        Returns:
            True if login form was submitted successfully,
            False otherwise.
        """

        field_mapping = {
            "username": {
                "selector": username_selector,
                "type": "text",
            },
            "password": {
                "selector": password_selector,
                "type": "text",
            },
        }

        form_data = {
            "username": username,
            "password": password,
        }

        return await self.fill_form(
            field_mapping,
            form_data,
            submit_selector,
        )
        
    # ---------------------------------------------------------------------------
    # Registration Forms
    # ---------------------------------------------------------------------------

    async def register(
        self,
        field_mapping: Dict[str, Dict[str, str]],
        user_data: Dict[str, Any],
        submit_selector: str,
    ) -> bool:
        """
        Fill and submit a registration form.

        Args:
            field_mapping:
                Registration form mapping.

            user_data:
                User information.

            submit_selector:
                Registration button selector.

        Returns:
            True on success, False otherwise.
        """

        return await self.fill_form(
            field_mapping,
            user_data,
            submit_selector,
        )
        
            # ---------------------------------------------------------------------------
    # Survey Forms
    # ---------------------------------------------------------------------------

    async def fill_survey(
        self,
        field_mapping: Dict[str, Dict[str, str]],
        survey_answers: Dict[str, Any],
        submit_selector: str,
    ) -> bool:
        """
        Fill and submit a survey form.

        Args:
            field_mapping:
                Survey field mapping.

            survey_answers:
                Survey answers.

            submit_selector:
                Submit button selector.

        Returns:
            True on success, False otherwise.
        """

        return await self.fill_form(
            field_mapping,
            survey_answers,
            submit_selector,
        )
        
            # ---------------------------------------------------------------------------
    # File Upload
    # ---------------------------------------------------------------------------

    async def upload_file(
        self,
        selector: str,
        file_path: str,
    ) -> bool:
        """
        Upload a file.

        Args:
            selector:
                File input selector.

            file_path:
                Path to file.

        Returns:
            True on success, False otherwise.
        """

        try:

            locator = self.page.locator(selector).first
            await locator.wait_for(state="visible")
            await locator.set_input_files(file_path)

            logger.info(f"Uploaded file: {file_path}")

            return True

        except Exception as exc:

            logger.error(f"File upload failed: {exc}")
            return False
        
        
            # ---------------------------------------------------------------------------
    # Job Application
    # ---------------------------------------------------------------------------

    async def apply_job(
        self,
        field_mapping: Dict[str, Dict[str, str]],
        applicant_data: Dict[str, Any],
        resume_selector: str,
        resume_path: str,
        submit_selector: str,
    ) -> bool:
        """
        Fill and submit a job application.

        Args:
            field_mapping:
                Job application mapping.

            applicant_data:
                Applicant information.

            resume_selector:
                Resume upload selector.

            resume_path:
                Resume file path.

            submit_selector:
                Submit button selector.

        Returns:
            True on success, False otherwise.
        """

        success = await self.fill_form(
            field_mapping,
            applicant_data,
        )

        if not success:
            return False

        success = await self.upload_file(
            resume_selector,
            resume_path,
        )

        if not success:
            return False

        return await actions.click(
            self.page,
            submit_selector,
        )
        
        
            # ---------------------------------------------------------------------------
    # Auto Fill
    # ---------------------------------------------------------------------------

    async def autofill_profile(
        self,
        profile_data: Dict[str, Any],
        field_mapping: Dict[str, Dict[str, str]],
    ) -> bool:
        """
        Auto-fill saved profile information.

        Args:
            profile_data:
                Saved user profile.

            field_mapping:
                Target form mapping.

        Returns:
            True on success, False otherwise.
        """

        return await self.fill_form(
            field_mapping,
            profile_data,
        )


    # ---------------------------------------------------------------------------
    # Zero-config Auto-fill (uses FormDiscovery internally)
    # ---------------------------------------------------------------------------

    async def autofill(
        self,
        user_data: Dict[str, Any],
        scope_selector: Optional[str] = None,
        submit: bool = False,
    ) -> bool:
        """
        Automatically discover all form fields on the current page and fill
        them using ``user_data`` — **no manual field mapping required**.

        The method uses ``FormDiscovery`` to scan the live DOM, infer field
        types and labels, map them to ``user_data`` keys via keyword matching
        and ``autocomplete`` attributes, then fills each matched field.

        Args:
            user_data:        Dict of user data (e.g. ``{"email": "jane@example.com"}``)
            scope_selector:   Optional CSS selector to limit scan to a container.
            submit:           If True, click the discovered submit button after filling.

        Returns:
            True if all matched fields were filled successfully, False otherwise.

        Example::

            filler = FormFiller(page)
            await filler.autofill({
                "email":      "jane@example.com",
                "first_name": "Jane",
                "last_name":  "Doe",
                "password":   "s3cur3P@ss!",
                "phone":      "+1-555-0100",
                "message":    "Hello, I'd like to learn more.",
            })
        """
        discovery = FormDiscovery()
        result: FormDiscoveryResult = await discovery.discover_and_map(
            self.page, user_data, scope_selector
        )

        if not result.all_fields:
            logger.warning("autofill: no form fields discovered on this page.")
            return False

        mapped_fields = [f for f in result.all_fields if f.mapped_to]
        if not mapped_fields:
            logger.warning("autofill: fields found but none could be mapped to user_data.")
            return False

        logger.info(
            f"autofill: filling {len(mapped_fields)} of {len(result.all_fields)} "
            f"discovered fields."
        )

        success = True
        for field in mapped_fields:
            value = user_data.get(field.mapped_to)
            if value is None:
                continue

            try:
                ft = field.field_type

                if ft in (FieldType.TEXT, FieldType.EMAIL, FieldType.PASSWORD,
                          FieldType.NUMBER, FieldType.TEL, FieldType.SEARCH,
                          FieldType.URL, FieldType.DATE):
                    ok = await actions.fill_field(self.page, field.selector, str(value))

                elif ft == FieldType.TEXTAREA:
                    ok = await actions.fill_field(self.page, field.selector, str(value))

                elif ft == FieldType.DROPDOWN:
                    ok = await actions.select_option(
                        self.page, field.selector, label=str(value)
                    )

                elif ft == FieldType.CHECKBOX:
                    ok = await actions.check_checkbox(
                        self.page, field.selector, checked=bool(value)
                    )

                elif ft == FieldType.RADIO:
                    ok = await actions.select_radio(
                        self.page,
                        field.group_name or field.name or "",
                        str(value),
                    )

                elif ft == FieldType.FILE:
                    locator = self.page.locator(field.selector).first
                    await locator.set_input_files(str(value))
                    ok = True

                else:
                    logger.debug(f"autofill: skipping unsupported type {ft} at {field.selector}")
                    ok = True

                if not ok:
                    logger.warning(
                        f"autofill: failed to fill [{field.field_type.value}] "
                        f"'{field.label}' ({field.selector})"
                    )
                    success = False

            except Exception as exc:
                logger.error(f"autofill: exception on {field.selector}: {exc}")
                success = False

        # Submit if requested
        if submit:
            for form in result.forms:
                if form.submit_selector:
                    await actions.click(self.page, form.submit_selector)
                    break

        return success

    async def discover_fields(
        self,
        scope_selector: Optional[str] = None,
    ) -> FormDiscoveryResult:
        """
        Discover all form fields on the current page without filling them.

        Returns a ``FormDiscoveryResult`` with the full structured field list.
        Useful for inspecting a form before deciding what data to supply.

        Args:
            scope_selector: Optional CSS selector to limit the scan.

        Returns:
            ``FormDiscoveryResult`` with all discovered fields.
        """
        discovery = FormDiscovery()
        return await discovery.discover(self.page, scope_selector)