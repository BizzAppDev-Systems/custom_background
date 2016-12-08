# -*- coding: utf-8 -*-
from openerp import models, fields, api
from openerp import SUPERUSER_ID
from openerp.exceptions import AccessError
from openerp.sql_db import TestCursor
from openerp.tools import config
from openerp.tools.misc import find_in_path
from openerp.tools.translate import _
from openerp.addons.web.http import request
from openerp.tools.safe_eval import safe_eval as eval
from openerp.exceptions import UserError

import re
import time
import base64
import logging
import tempfile
import lxml.html
import os
import subprocess
from contextlib import closing
from distutils.version import LooseVersion
from functools import partial
from pyPdf import PdfFileWriter, PdfFileReader
from reportlab.graphics.barcode import createBarcodeDrawing

try:
    createBarcodeDrawing('Code128', value='foo', format='png', width=100, height=100, humanReadable=1).asString('png')
except Exception:
    pass


#--------------------------------------------------------------------------
# Helpers
#--------------------------------------------------------------------------
_logger = logging.getLogger(__name__)

def _get_wkhtmltopdf_bin():
    return find_in_path('wkhtmltopdf')


class ir_actions_report_xml(models.Model):
    _inherit = "ir.actions.report.xml"

    custom_report_background = fields.Boolean(string='Custom Report Background')

class Report(models.Model):
    _inherit = 'report'

    @api.v7
    def get_pdf(self, cr, uid, ids, report_name, html=None, data=None, context=None):
        """This method generates and returns pdf version of a report.
        """
        print "22222222222222222222222222", report_name, ids, context
        if context is None:
            context = {}

        # As the assets are generated during the same transaction as the rendering of the
        # templates calling them, there is a scenario where the assets are unreachable: when
        # you make a request to read the assets while the transaction creating them is not done.
        # Indeed, when you make an asset request, the controller has to read the `ir.attachment`
        # table.
        # This scenario happens when you want to print a PDF report for the first time, as the
        # assets are not in cache and must be generated. To workaround this issue, we manually
        # commit the writes in the `ir.attachment` table. It is done thanks to a key in the context.
        if not config['test_enable']:
            context = dict(context, commit_assetsbundle=True)

        if html is None:
            html = self.get_html(cr, uid, ids, report_name, data=data, context=context)

        # The test cursor prevents the use of another environnment while the current
        # transaction is not finished, leading to a deadlock when the report requests
        # an asset bundle during the execution of test scenarios. In this case, return
        # the html version.
        if isinstance(cr, TestCursor):
            return html

        html = html.decode('utf-8')  # Ensure the current document is utf-8 encoded.

        # Get the ir.actions.report.xml record we are working on.
        report = self._get_report_from_name(cr, uid, report_name)
        print "22211111111111111111!", report
        # Check if we have to save the report or if we have to get one from the db.
        save_in_attachment = self._check_attachment_use(cr, uid, ids, report)
        # Get the paperformat associated to the report, otherwise fallback on the company one.
        if not report.paperformat_id:
            user = self.pool['res.users'].browse(cr, uid, uid)
            paperformat = user.company_id.paperformat_id
        else:
            paperformat = report.paperformat_id

        # Preparing the minimal html pages
        headerhtml = []
        contenthtml = []
        footerhtml = []
        irconfig_obj = self.pool['ir.config_parameter']
        base_url = irconfig_obj.get_param(cr, SUPERUSER_ID, 'report.url') or irconfig_obj.get_param(cr, SUPERUSER_ID, 'web.base.url')

        # Minimal page renderer
        view_obj = self.pool['ir.ui.view']
        render_minimal = partial(view_obj.render, cr, uid, 'report.minimal_layout', context=context)

        # The received html report must be simplified. We convert it in a xml tree
        # in order to extract headers, bodies and footers.
        try:
            root = lxml.html.fromstring(html)
            match_klass = "//div[contains(concat(' ', normalize-space(@class), ' '), ' {} ')]"

            for node in root.xpath(match_klass.format('header')):
                body = lxml.html.tostring(node)
                header = render_minimal(dict(subst=True, body=body, base_url=base_url))
                headerhtml.append(header)

            for node in root.xpath(match_klass.format('footer')):
                body = lxml.html.tostring(node)
                footer = render_minimal(dict(subst=True, body=body, base_url=base_url))
                footerhtml.append(footer)

            for node in root.xpath(match_klass.format('page')):
                # Previously, we marked some reports to be saved in attachment via their ids, so we
                # must set a relation between report ids and report's content. We use the QWeb
                # branding in order to do so: searching after a node having a data-oe-model
                # attribute with the value of the current report model and read its oe-id attribute
                if ids and len(ids) == 1:
                    reportid = ids[0]
                else:
                    oemodelnode = node.find(".//*[@data-oe-model='%s']" % report.model)
                    if oemodelnode is not None:
                        reportid = oemodelnode.get('data-oe-id')
                        if reportid:
                            reportid = int(reportid)
                    else:
                        reportid = False

                # Extract the body
                body = lxml.html.tostring(node)
                reportcontent = render_minimal(dict(subst=False, body=body, base_url=base_url))

                contenthtml.append(tuple([reportid, reportcontent]))

        except lxml.etree.XMLSyntaxError:
            contenthtml = []
            contenthtml.append(html)
            save_in_attachment = {}  # Don't save this potentially malformed document

        # Get paperformat arguments set in the root html tag. They are prioritized over
        # paperformat-record arguments.
        specific_paperformat_args = {}
        for attribute in root.items():
            if attribute[0].startswith('data-report-'):
                specific_paperformat_args[attribute[0]] = attribute[1]

        # Run wkhtmltopdf process
        return self._run_wkhtmltopdf(
            cr, uid, headerhtml, footerhtml, contenthtml, context.get('landscape'),
            paperformat, specific_paperformat_args, save_in_attachment,
            context.get('set_viewport_size'), report.custom_report_background
        )
    def _run_wkhtmltopdf(self, cr, uid, headers, footers, bodies, landscape,
                         paperformat, spec_paperformat_args=None,
                         save_in_attachment=None, set_viewport_size=False,
                         custom_report_background=False):
        """Execute wkhtmltopdf as a subprocess in order to convert html given in input into a pdf
        document.

        :param header: list of string containing the headers
        :param footer: list of string containing the footers
        :param bodies: list of string containing the reports
        :param landscape: boolean to force the pdf to be rendered under a landscape format
        :param paperformat: ir.actions.report.paperformat to generate the wkhtmltopf arguments
        :param specific_paperformat_args: dict of prioritized paperformat arguments
        :param save_in_attachment: dict of reports to save/load in/from the db
        :returns: Content of the pdf as a string
        """
        if not save_in_attachment:
            save_in_attachment = {}

        command_args = []
        if set_viewport_size:
            command_args.extend(['--viewport-size', landscape and '1024x1280' or '1280x1024'])

        # Passing the cookie to wkhtmltopdf in order to resolve internal links.
        try:
            if request:
                command_args.extend(['--cookie', 'session_id', request.session.sid])
        except AttributeError:
            pass

        # Wkhtmltopdf arguments
        command_args.extend(['--quiet'])  # Less verbose error messages
        if paperformat:
            # Convert the paperformat record into arguments
            command_args.extend(self._build_wkhtmltopdf_args(paperformat, spec_paperformat_args))

        # Force the landscape orientation if necessary
        if landscape and '--orientation' in command_args:
            command_args_copy = list(command_args)
            for index, elem in enumerate(command_args_copy):
                if elem == '--orientation':
                    del command_args[index]
                    del command_args[index]
                    command_args.extend(['--orientation', 'landscape'])
        elif landscape and '--orientation' not in command_args:
            command_args.extend(['--orientation', 'landscape'])

        # Execute WKhtmltopdf
        pdfdocuments = []
        temporary_files = []

        for index, reporthtml in enumerate(bodies):
            local_command_args = []
            pdfreport_fd, pdfreport_path = tempfile.mkstemp(suffix='.pdf', prefix='report.tmp.')
            temporary_files.append(pdfreport_path)

            # Directly load the document if we already have it
            if save_in_attachment and save_in_attachment['loaded_documents'].get(reporthtml[0]):
                with closing(os.fdopen(pdfreport_fd, 'w')) as pdfreport:
                    pdfreport.write(save_in_attachment['loaded_documents'][reporthtml[0]])
                pdfdocuments.append(pdfreport_path)
                continue
            else:
                os.close(pdfreport_fd)

            # Wkhtmltopdf handles header/footer as separate pages. Create them if necessary.
            if headers:
                head_file_fd, head_file_path = tempfile.mkstemp(suffix='.html', prefix='report.header.tmp.')
                temporary_files.append(head_file_path)
                with closing(os.fdopen(head_file_fd, 'w')) as head_file:
                    head_file.write(headers[index])
                local_command_args.extend(['--header-html', head_file_path])
            if footers:
                foot_file_fd, foot_file_path = tempfile.mkstemp(suffix='.html', prefix='report.footer.tmp.')
                temporary_files.append(foot_file_path)
                with closing(os.fdopen(foot_file_fd, 'w')) as foot_file:
                    foot_file.write(footers[index])
                local_command_args.extend(['--footer-html', foot_file_path])

            # Body stuff
            content_file_fd, content_file_path = tempfile.mkstemp(suffix='.html', prefix='report.body.tmp.')
            temporary_files.append(content_file_path)
            with closing(os.fdopen(content_file_fd, 'w')) as content_file:
                content_file.write(reporthtml[1])

            try:
                wkhtmltopdf = [_get_wkhtmltopdf_bin()] + command_args + local_command_args
                wkhtmltopdf += [content_file_path] + [pdfreport_path]
                process = subprocess.Popen(wkhtmltopdf, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                out, err = process.communicate()

                if process.returncode not in [0, 1]:
                    raise UserError(_('Wkhtmltopdf failed (error code: %s). '
                                      'Message: %s') % (str(process.returncode), err))

                # Save the pdf in attachment if marked
                if reporthtml[0] is not False and save_in_attachment.get(reporthtml[0]):
                    with open(pdfreport_path, 'rb') as pdfreport:
                        attachment = {
                            'name': save_in_attachment.get(reporthtml[0]),
                            'datas': base64.encodestring(pdfreport.read()),
                            'datas_fname': save_in_attachment.get(reporthtml[0]),
                            'res_model': save_in_attachment.get('model'),
                            'res_id': reporthtml[0],
                        }
                        try:
                            self.pool['ir.attachment'].create(cr, uid, attachment)
                        except AccessError:
                            _logger.info("Cannot save PDF report %r as attachment", attachment['name'])
                        else:
                            _logger.info('The PDF document %s is now saved in the database',
                                         attachment['name'])

                if custom_report_background:
                    temp_back_id, temp_back_path = tempfile.mkstemp(suffix='.pdf', prefix='back_report.tmp.')
                    user = self.pool['res.users'].browse(cr, uid, uid)
                    back_data = base64.decodestring(user.company_id.custom_report_background_image)
                    with closing(os.fdopen(temp_back_id, 'w')) as back_file:
                        back_file.write(back_data)
                    os.system("pdftk "+ pdfreport_path + " background " +
                              temp_back_path +"  output "+ pdfreport_path.replace('report', 'with_back_report'))
                    pdfreport_path = pdfreport_path.replace('report', 'with_back_report')
                pdfdocuments.append(pdfreport_path)
            except:
                raise

        # Return the entire document
        if len(pdfdocuments) == 1:
            entire_report_path = pdfdocuments[0]
        else:
            entire_report_path = self._merge_pdf(pdfdocuments)
            temporary_files.append(entire_report_path)

        with open(entire_report_path, 'rb') as pdfdocument:
            content = pdfdocument.read()

        # Manual cleanup of the temporary files
        for temporary_file in temporary_files:
            try:
                os.unlink(temporary_file)
            except (OSError, IOError):
                _logger.error('Error when trying to remove file %s' % temporary_file)

        return content

# vim:expandtab:smartindent:tabstop=4:softtabstop=4:shiftwidth=4:
