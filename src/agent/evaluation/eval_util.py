import os
import re
import json
import logging
import pandas as pd
from llama_index.core.workflow import Workflow
from tqdm import tqdm
from sklearn.metrics import classification_report, confusion_matrix
import matplotlib.pyplot as plt
from matplotlib.patches import Patch
import seaborn as sns
import evaluate
from transformers import pipeline
import torch
import numpy as np

class Evaluation_Utility():
    """
    A utility class for evaluating AITA classifications and justifications.
    """
    
    def __init__(self):
        self.logger = logging.getLogger(__name__)
        self.AITA_classifications = ['NTA', 'YTA', 'NAH', 'ESH']
        self.logger.info("Initialized Evaluation_Utility")

    def create_test_set(
        self, 
        df: pd.DataFrame, 
        sampling: str = 'full', 
        balanced_samples_per_class: int = None,
        weighted_total_samples: int = None
    ) -> list[dict]:
        """
        Create a test set from a DataFrame containing test data with options for different sampling strategies.
        
        Args:
            df (pd.DataFrame): DataFrame containing the test dataset
            sampling (str): Sampling strategy to use:
                - 'full': Use entire dataset (default)
                - 'balanced': Equal samples per class
                - 'weighted': Maintain class proportions with reduced size
            samples_per_class (int, optional): Number of samples per class when sampling='balanced'
            total_samples (int, optional): Total number of samples when sampling='weighted'
            
        Returns:
            list[dict]: Test set containing query, top comment, and classification
        """
        self.logger.info(f"Creating test set using {sampling} sampling strategy")
        
        try:
            if sampling == 'balanced':
                if balanced_samples_per_class is None:
                    balanced_samples_per_class = 10
                self.logger.info(f"Creating balanced test set with {balanced_samples_per_class} samples per class")
                df = (df.groupby('top_comment_1_classification')
                    .apply(lambda x: x.sample(n=min(len(x), balanced_samples_per_class)))
                    .reset_index(drop=True))
                self.logger.debug(f"Class distribution in balanced set: {df['top_comment_1_classification'].value_counts()}")
                
            elif sampling == 'weighted':
                if weighted_total_samples is None:
                    raise ValueError("total_samples must be specified when using weighted sampling")
                if weighted_total_samples > len(df):
                    raise ValueError("total_samples cannot be larger than the input dataset")
                    
                # Calculate class proportions
                class_proportions = df['top_comment_1_classification'].value_counts(normalize=True)
                
                # Calculate samples per class while maintaining proportions
                class_samples = (class_proportions * weighted_total_samples).round().astype(int)
                
                # Adjust for rounding errors to match total_samples exactly
                diff = weighted_total_samples - class_samples.sum()
                if diff != 0:
                    # Add/subtract the difference from the largest class
                    largest_class = class_samples.index[0]
                    class_samples[largest_class] += diff
                
                # Sample from each class according to calculated proportions
                sampled_df = pd.DataFrame()
                for class_label, n_samples in class_samples.items():
                    class_df = df[df['top_comment_1_classification'] == class_label]
                    sampled_df = pd.concat([
                        sampled_df,
                        class_df.sample(n=min(len(class_df), n_samples))
                    ])
                
                df = sampled_df.reset_index(drop=True)
                
                self.logger.info(f"Created weighted test set with {len(df)} total samples")
                self.logger.debug(f"Class distribution in weighted set: {df['top_comment_1_classification'].value_counts()}")
                self.logger.debug(f"Class proportions in weighted set: {df['top_comment_1_classification'].value_counts(normalize=True)}")
                
            else:  # 'full' sampling
                self.logger.info(f"Creating full test set from DataFrame with {len(df)} rows")

            test_set = []
            for _, row in df.iterrows():
                test_set.append({
                    'query': row['submission_title'] + '\n\n' + row['submission_text'],
                    'top_comment': row['top_comment_1'],
                    'top_comment_classification': row['top_comment_1_classification']
                })
            
            self.logger.info(f"Successfully created test set with {len(test_set)} samples")
            return test_set
            
        except Exception as e:
            self.logger.error(f"Error creating test set: {str(e)}", exc_info=True)
            raise

    async def collect_responses(self, workflow: Workflow, test_set: list[tuple]):
        """Collect responses from the workflow for each sample in the test set."""
        self.logger.info(f"Starting response collection for {len(test_set)} samples")
        test_responses = []
        error_count = 0

        for i in tqdm(range(len(test_set)), desc='Generating agent responses on test set.'):
            try:
                self.logger.debug(f"Processing sample {i+1}/{len(test_set)}")
                result = await workflow.run(query=test_set[i]['query'])

                retrieved_doc_contents = []
                for node in result.source_nodes:
                    retrieved_doc_contents.append({
                        'text': node.text,
                        'classification': node.metadata['Correct Classification'],
                        'justification': node.metadata['Correct Justification']
                    })
                
                response = ""
                async for chunk in result.async_response_gen():
                    response += chunk

                test_responses.append({
                    'response': response,
                    'query': test_set[i]['query'],
                    'retrieved_docs': retrieved_doc_contents,
                    'top_comment': test_set[i]['top_comment'],
                    'top_comment_classification': test_set[i]['top_comment_classification']
                })
                
                self.logger.debug(f"Successfully processed sample {i+1}")
                
            except Exception as e:
                error_count += 1
                self.logger.warning(f"Error processing sample {i}: {str(e)}")
                continue

        self.logger.info(f"Response collection completed. Successful: {len(test_responses)}, Failed: {error_count}")
        return test_responses

    def parse_AITA_classification(self, response: str, parse_type: str = 'response') -> str:
        """Parse the AITA classification from a response string."""
        try:
            if parse_type == 'response':
                pattern = '|'.join(self.AITA_classifications)
                match = re.search(pattern, response)

                if match:
                    return match.group(0)
                self.logger.debug("No classification found in response text")
                return ''
            
            elif parse_type == 'doc':
                justification_pos = response.find('Correct Classification:')

                if justification_pos != -1:
                    substring_after = response[justification_pos:]
                    pattern = '|'.join(self.AITA_classifications)
                    match = re.search(pattern, substring_after)
                    
                    if match:
                        return match.group(0)
                self.logger.debug("No classification found in document text")
                return ''
            
            else:
                raise ValueError(f"Invalid parse type: {parse_type}")
                
        except Exception as e:
            self.logger.error(f"Error parsing AITA classification: {str(e)}", exc_info=True)
            raise

    def evaluate(self, responses: list[dict], results_directory: str,
                classification_report_filepath: str, confusion_matrix_filepath: str,
                mcc_filepath: str, rouge_filepath: str, bleu_filepath: str,
                comet_filepath: str, toxicity_stats_filepath: str,
                toxicity_plot_filepath: str, retrieval_eval_filepath: str,
                retrieval_eval_summary_filepath: str):
        """Evaluate classifications and justifications from the responses."""
        self.logger.info("Starting comprehensive evaluation")
        
        try:
            self.logger.info("Evaluating classifications...")
            self.evaluate_classifications(
                responses=responses,
                results_directory=results_directory,
                classification_report_filepath=classification_report_filepath,
                confusion_matrix_filepath=confusion_matrix_filepath,
                mcc_filepath=mcc_filepath
            )

            self.logger.info("Evaluating justifications...")
            self.evaluate_justifications(
                responses=responses,
                results_directory=results_directory,
                rouge_filepath=rouge_filepath,
                bleu_filepath=bleu_filepath,
                comet_filepath=comet_filepath,
                toxicity_stats_filepath=toxicity_stats_filepath,
                toxicity_plot_filepath=toxicity_plot_filepath
            )
            
            self.logger.info("Evaluating retrieval...")
            self.evaluate_retrieval(
                responses=responses,
                results_directory=results_directory,
                retrieval_eval_filepath=retrieval_eval_filepath,
                retrieval_eval_summary_filepath=retrieval_eval_summary_filepath
            )
            
            self.logger.info("Comprehensive evaluation completed successfully")
            
        except Exception as e:
            self.logger.error(f"Error during comprehensive evaluation: {str(e)}", exc_info=True)
            raise

    def evaluate_classifications(self, responses: list[dict], results_directory: str,
                               classification_report_filepath: str, 
                               confusion_matrix_filepath: str, mcc_filepath: str):
        """Evaluate AITA classifications using multiple metrics."""
        self.logger.info("Starting classification evaluation")
        
        try:
            # Parse classifications from responses
            self.logger.debug("Extracting classifications from responses")
            for response in responses:
                response['predicted_classification'] = self.parse_AITA_classification(response['response'])

            true_labels = [response['top_comment_classification'] for response in responses]
            predicted_labels = [response['predicted_classification'] for response in responses]

            # Generate classification report
            self.logger.info("Generating classification report")
            classification_metrics = classification_report(
                true_labels, 
                predicted_labels, 
                labels=self.AITA_classifications, 
                zero_division=0
            )
            report_path = os.path.join(results_directory, classification_report_filepath)
            with open(report_path, 'w') as f:
                f.write(classification_metrics)
            self.logger.debug(f"Classification report saved to {report_path}")

            # Generate confusion matrix
            self.logger.info("Generating confusion matrix")
            plt.figure(figsize=(10, 8))
            cm = confusion_matrix(
                true_labels, 
                predicted_labels, 
                labels=self.AITA_classifications
            )
            plt.title('AITA Agent Classifications')
            sns.heatmap(
                cm, 
                annot=True, 
                fmt="d", 
                cmap="Blues",
                xticklabels=self.AITA_classifications,
                yticklabels=self.AITA_classifications,
                annot_kws={"size": 28}
            )
            plt.xlabel('Predicted', fontsize=28)
            plt.ylabel('True', fontsize=28)
            plt.xticks(fontsize=28)
            plt.yticks(fontsize=28)
            
            matrix_path = os.path.join(results_directory, confusion_matrix_filepath)
            plt.savefig(matrix_path)
            plt.close()
            self.logger.debug(f"Confusion matrix saved to {matrix_path}")

            # Calculate Matthews Correlation Coefficient
            self.logger.info("Calculating Matthews Correlation Coefficient")
            matthews_metric = evaluate.load("matthews_correlation")
            mcc = matthews_metric.compute(
                references=[self.AITA_classifications.index(x) for x in true_labels],
                predictions=[self.AITA_classifications.index(x) for x in predicted_labels]
            )
            
            mcc_path = os.path.join(results_directory, mcc_filepath)
            with open(mcc_path, 'w') as f:
                json.dump({'mcc': mcc}, f)
            self.logger.debug(f"MCC score saved to {mcc_path}")
            
            self.logger.info("Classification evaluation completed successfully")
            
        except Exception as e:
            self.logger.error(f"Error during classification evaluation: {str(e)}", exc_info=True)
            raise

    def evaluate_justifications(self, responses: list[dict], results_directory: str,
                              rouge_filepath: str, bleu_filepath: str,
                              comet_filepath: str, toxicity_stats_filepath: str,
                              toxicity_plot_filepath: str):
        """Evaluate response justifications using multiple metrics."""
        self.logger.info("Starting justification evaluation")
        
        try:
            predictions = [response['response'] for response in responses]
            references = [response['top_comment'] for response in responses]
            sources = [response['query'] for response in responses]

            # Calculate ROUGE scores
            self.logger.info("Calculating ROUGE scores")
            rouge_metric = evaluate.load("rouge")
            rouge = rouge_metric.compute(predictions=predictions, references=references)
            rouge_path = os.path.join(results_directory, rouge_filepath)
            with open(rouge_path, 'w') as f:
                json.dump(rouge, f)
            self.logger.debug(f"ROUGE scores saved to {rouge_path}")

            # Calculate BLEU scores
            self.logger.info("Calculating BLEU scores")
            bleu_metric = evaluate.load("bleu")
            bleu = bleu_metric.compute(predictions=predictions, references=references)
            bleu_path = os.path.join(results_directory, bleu_filepath)
            with open(bleu_path, 'w') as f:
                json.dump(bleu, f)
            self.logger.debug(f"BLEU scores saved to {bleu_path}")

            # Calculate COMET scores
            self.logger.info("Calculating COMET scores")
            comet_metric = evaluate.load('comet')
            comet_score = comet_metric.compute(predictions=predictions, references=references, sources=sources)
            comet_path = os.path.join(results_directory, comet_filepath)
            with open(comet_path, 'w') as f:
                json.dump(comet_score, f)

            # Toxicity analysis
            self.logger.info("Starting toxicity analysis")
            device = 'cuda' if torch.cuda.is_available() else 'cpu'
            self.logger.debug(f"Using device: {device} for toxicity model")
            
            toxicity_model = pipeline(
                "text-classification", 
                model="tomh/toxigen_roberta", 
                truncation=True, 
                device_map=device
            )
            
            # Process responses for toxicity
            self.logger.debug("Analyzing toxicity of responses")
            for response in responses:
                predicted_response_score = toxicity_model(response['response'])[0]
                predicted_response_score['label'] = ('BENIGN' if predicted_response_score['label'] == 'LABEL_0' 
                                                   else 'TOXIC')
                response['predicted_response_toxicity'] = predicted_response_score

                true_response_score = toxicity_model(response['top_comment'])[0]
                true_response_score['label'] = ('BENIGN' if true_response_score['label'] == 'LABEL_0' 
                                              else 'TOXIC')
                response['true_response_toxicity'] = true_response_score

            # Calculate toxicity statistics
            self.logger.info("Calculating toxicity statistics")
            predicted_toxicity_scores = [
                {'score': r['predicted_response_toxicity']['score'],
                 'label': r['predicted_response_toxicity']['label']}
                for r in responses
            ]
            true_toxicity_scores = [
                {'score': r['true_response_toxicity']['score'],
                 'label': r['true_response_toxicity']['label']}
                for r in responses
            ]

            predicted_toxicity_scores = [
                -s['score'] if s['label'] == 'TOXIC' else s['score'] 
                for s in predicted_toxicity_scores
            ]
            true_toxicity_scores = [
                -s['score'] if s['label'] == 'TOXIC' else s['score']
                for s in true_toxicity_scores
            ]

            # Generate and save toxicity statistics
            toxicity_stats = self.get_toxicity_stats(predicted_toxicity_scores, true_toxicity_scores)
            stats_path = os.path.join(results_directory, toxicity_stats_filepath)
            with open(stats_path, 'w') as f:
                json.dump(toxicity_stats, f)
            self.logger.debug(f"Toxicity statistics saved to {stats_path}")

            # Generate toxicity plot
            self.logger.info("Generating toxicity visualization")
            self.plot_toxicity_scores(
                predicted_toxicity_scores,
                true_toxicity_scores,
                os.path.join(results_directory, toxicity_plot_filepath),
                toxicity_stats
            )
            
            self.logger.info("Justification evaluation completed successfully")
            
        except Exception as e:
            self.logger.error(f"Error during justification evaluation: {str(e)}", exc_info=True)
            raise

    def get_toxicity_stats(self, predicted_toxicity_scores: list[float],
                          true_toxicity_scores: list[float]) -> dict:
        """Compute statistics for toxicity scores."""
        self.logger.debug("Computing toxicity statistics")
        try:
            # Calculate predicted scores statistics
            predicted_mean_score = round(np.mean(predicted_toxicity_scores), 3)
            predicted_median_score = round(np.median(predicted_toxicity_scores), 3)
            predicted_benign_count = sum(1 for s in predicted_toxicity_scores if s >= 0)
            predicted_toxic_count = sum(1 for s in predicted_toxicity_scores if s < 0)

            # Calculate true scores statistics
            true_mean_score = round(np.mean(true_toxicity_scores), 3)
            true_median_score = round(np.median(true_toxicity_scores), 3)
            true_benign_count = sum(1 for s in true_toxicity_scores if s >= 0)
            true_toxic_count = sum(1 for s in true_toxicity_scores if s < 0)

            # Calculate changes
            percent_change_mean = round(((predicted_mean_score - true_mean_score) / true_mean_score) * 100, 2)
            percent_change_median = round(((predicted_median_score - true_median_score) / true_median_score) * 100, 2)
            total_change_benign = predicted_benign_count - true_benign_count
            total_change_toxic = predicted_toxic_count - true_toxic_count
            
            percent_change_benign = ('N/A' if true_benign_count == 0 else 
                                   round(((predicted_benign_count - true_benign_count) / true_benign_count) * 100, 2))
            percent_change_toxic = ('N/A' if true_toxic_count == 0 else 
                                  round(((predicted_toxic_count - true_toxic_count) / true_toxic_count) * 100, 2))

            stats = {
                'predicted_response_toxicity_stats': {
                    'mean_score': predicted_mean_score,
                    'median_score': predicted_median_score,
                    'benign_count': predicted_benign_count,
                    'toxic_count': predicted_toxic_count,
                },
                'true_response_toxicity_stats': {
                    'mean_score': true_mean_score,
                    'median_score': true_median_score,
                    'benign_count': true_benign_count,
                    'toxic_count': true_toxic_count,
                },
                'change_stats': {
                    'percent_change_mean': percent_change_mean,
                    'percent_change_median': percent_change_median,
                    'total_change_benign': total_change_benign,
                    'total_change_toxic': total_change_toxic,
                    'percent_change_benign': percent_change_benign,
                    'percent_change_toxic': percent_change_toxic,
                }
            }
            
            self.logger.debug("Toxicity statistics computed successfully")
            return stats
            
        except Exception as e:
            self.logger.error(f"Error computing toxicity statistics: {str(e)}", exc_info=True)
            raise

    def plot_toxicity_scores(self, predicted_toxicity_scores: list[float],
                            true_toxicity_scores: list[float],
                            toxicity_plot_filepath: str,
                            toxicity_stats: dict,
                            bins: int = 40,
                            figsize: tuple = (20, 7),
                            labels: tuple = ("Predicted Responses", "True Responses")):
        """Create a histogram of toxicity scores."""
        self.logger.info("Generating toxicity score visualization")
        
        try:
            # Create the figure and axis
            fig, ax = plt.subplots(figsize=figsize)
            
            # Define colors for the plots
            colors = {
                'hist1': 'skyblue',
                'hist2': 'lightgreen',
            }
            
            # Create histograms
            self.logger.debug("Plotting histograms")
            plt.hist(
                predicted_toxicity_scores,
                bins=bins,
                edgecolor='black',
                linewidth=1.2,
                alpha=0.5,
                color=colors['hist1'],
                label=f'Histogram ({labels[0]})'
            )
            
            plt.hist(
                true_toxicity_scores,
                bins=bins,
                edgecolor='black',
                linewidth=1.2,
                alpha=0.5,
                color=colors['hist2'],
                label=f'Histogram ({labels[1]})'
            )
            
            # Customize the plot
            self.logger.debug("Adding plot customizations")
            ax.set_xlabel('Confidence Score', fontsize=14, labelpad=10)
            ax.set_ylabel('Count', fontsize=14)
            ax.set_title('Confidence Scores for Response Toxicity', fontsize=16, pad=20)
            plt.axvline(x=0, color='black', linestyle='--', alpha=0.5, linewidth=2)
            plt.xlim(-1, 1)
            plt.xticks(np.arange(-1, 1.2, 0.2))
            plt.grid(True, alpha=0.3, linestyle='--')
            
            # Add labels and legends
            plt.text(-0.6, plt.gca().get_ylim()[1]*0.95, 'TOXIC', fontsize=14)
            plt.text(0.4, plt.gca().get_ylim()[1]*0.95, 'BENIGN', fontsize=14)
            
            # Create legends
            self.logger.debug("Adding legends")
            legend1_elements = [
                Patch(facecolor=colors['hist1'], alpha=0.5, edgecolor='black', label=labels[0]),
                Patch(facecolor='none', label=f"Mean: {toxicity_stats['predicted_response_toxicity_stats']['mean_score']:.2f}"),
                Patch(facecolor='none', label=f"Median: {toxicity_stats['predicted_response_toxicity_stats']['median_score']:.2f}"),
                Patch(facecolor='none', label=f"Benign Responses: {toxicity_stats['predicted_response_toxicity_stats']['benign_count']}"),
                Patch(facecolor='none', label=f"Toxic Responses: {toxicity_stats['predicted_response_toxicity_stats']['toxic_count']}")
            ]
            
            legend2_elements = [
                Patch(facecolor=colors['hist2'], alpha=0.5, edgecolor='black', label=labels[1]),
                Patch(facecolor='none', label=f"Mean: {toxicity_stats['true_response_toxicity_stats']['mean_score']:.2f}"),
                Patch(facecolor='none', label=f"Median: {toxicity_stats['true_response_toxicity_stats']['median_score']:.2f}"),
                Patch(facecolor='none', label=f"Benign Responses: {toxicity_stats['true_response_toxicity_stats']['benign_count']}"),
                Patch(facecolor='none', label=f"Toxic Responses: {toxicity_stats['true_response_toxicity_stats']['toxic_count']}")
            ]
            
            # Add legends to plot
            legend1 = plt.legend(handles=legend1_elements, bbox_to_anchor=(1.02, 1), 
                               loc='upper left', prop={'size': 12})
            plt.gca().add_artist(legend1)
            plt.legend(handles=legend2_elements, bbox_to_anchor=(1.02, 0.75), 
                      loc='upper left', prop={'size': 12})
            
            # Add explanatory note
            plt.figtext(0.7475, 0.49, 
                       '*Negative scores indicate\ntoxic classifications while\n positive ones are benign.',
                       fontsize=12, ha='center', va='top')
            
            # Adjust layout and save
            plt.tight_layout(rect=[0, 0, 0.82, 1])
            plt.savefig(toxicity_plot_filepath, dpi=300, bbox_inches='tight')
            plt.close()
            
            self.logger.info(f"Toxicity plot saved to {toxicity_plot_filepath}")
            
        except Exception as e:
            self.logger.error(f"Error generating toxicity plot: {str(e)}", exc_info=True)
            raise

    def evaluate_retrieval(self, responses: list[dict], results_directory: str,
                          retrieval_eval_filepath: str, retrieval_eval_summary_filepath: str):
        """Evaluate the quality of document retrieval for AITA classifications."""
        self.logger.info("Starting retrieval evaluation")
        
        try:
            retrieval_evaluations = []
            total_responses = len(responses)
            
            self.logger.debug(f"Processing {total_responses} responses for retrieval evaluation")
            for idx, response in enumerate(responses, 1):
                if idx % 50 == 0:
                    self.logger.debug(f"Processed {idx}/{total_responses} responses")
                    
                # Get true classification and document classifications
                true_classification = response['top_comment_classification']
                doc_classifications = [doc['classification'] for doc in response['retrieved_docs']]
                
                # Get top retrieved doc classification
                top_doc_classification = doc_classifications[0]

                # Count classifications
                classification_counts = {c: 0 for c in self.AITA_classifications}
                for classification in doc_classifications:
                    classification_counts[classification] += 1
                
                # Calculate match ratio
                correct_classification_ratio = (classification_counts[true_classification] / 
                                             len(doc_classifications))

                # Store results
                retrieval_evaluations.append({
                    'true_class': true_classification,
                    'top_doc_class': top_doc_classification,
                    'class_counts': classification_counts,
                    'doc_class_match_accuracy': correct_classification_ratio
                })

            # Calculate summary metrics
            self.logger.info("Calculating retrieval summary metrics")
            summary_results = {
                'avg_top_doc_class_match_accuracy': sum(
                    x['top_doc_class'] == x['true_class'] for x in retrieval_evaluations
                ) / len(retrieval_evaluations),
                'avg_doc_class_match_accuracy': sum(
                    x['doc_class_match_accuracy'] for x in retrieval_evaluations
                ) / len(retrieval_evaluations)
            }

            # Save detailed results
            self.logger.debug("Saving detailed retrieval evaluation")
            eval_path = os.path.join(results_directory, retrieval_eval_filepath)
            with open(eval_path, 'w') as f:
                json.dump(retrieval_evaluations, f)
            self.logger.debug(f"Detailed retrieval evaluation saved to {eval_path}")
            
            # Save summary results
            self.logger.debug("Saving retrieval summary metrics")
            summary_path = os.path.join(results_directory, retrieval_eval_summary_filepath)
            with open(summary_path, 'w') as f:
                json.dump(summary_results, f)
            self.logger.debug(f"Retrieval summary metrics saved to {summary_path}")
            
            self.logger.info("Retrieval evaluation completed successfully")
            self.logger.info(f"Top document classification accuracy: {summary_results['avg_top_doc_class_match_accuracy']:.2%}")
            self.logger.info(f"Average document classification accuracy: {summary_results['avg_doc_class_match_accuracy']:.2%}")
            
        except Exception as e:
            self.logger.error(f"Error during retrieval evaluation: {str(e)}", exc_info=True)
            raise